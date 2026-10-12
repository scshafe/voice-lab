"""Training model families on the corpus: the adapter port and F5-TTS's adapter (D-049).

@canonical training -- a model family's training is one ``TrainingAdapter``: it builds the
family's settings (``hyperparameters``, ``listening_settings``) from options, builds the
family's dataset from a corpus selection (its sample rate, normalisation and transcript form;
the corpus itself, 48 kHz 24-bit mono takes with their prompt text, belongs to no family),
describes and checks the family's isolated runtime (one hash-locked environment with pinned
weights, D-004), launches the training there and records the run (``finetune.FineTuneRun``,
with the runtime's identity), finds the checkpoints it wrote, reads its progress from the
training log, and makes the listening samples, choosing their reference itself when the
family has one. ``finetune.run_finetune_pipeline`` and the command line go through the
adapter of the hyperparameters' family (``adapter_for``); ``require_trains`` holds a given
adapter to them before anything is read; ``run_family`` names the family of a stored run.

F5-TTS is the first adapter (``F5_TRAINING``), over the existing F5-TTS functions of
``finetune``: its records, files and behaviour are those of every run trained so far. A run's
family is read from its hyperparameters: F5-TTS's keep their own schema
(``FineTuneHyperparameters``), another family's name themselves (``FamilyHyperparameters``).
Adding a family, and what common code still holds of F5-TTS's shape: docs/MODEL_FAMILIES.md.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal, Protocol

from pydantic import BaseModel, Field

from .errors import VoiceLabError
from .finetune import (
    FINETUNE_DEFAULT_SAMPLE_TEXTS,
    FINETUNE_FFMPEG,
    FINETUNE_SAMPLE_RATE_HZ,
    F5Device,
    FamilyHyperparameters,
    FineTuneCheckpoint,
    FineTuneCorpus,
    FineTuneDataset,
    FineTuneDigestCache,
    FineTuneError,
    FineTuneHyperparameters,
    FineTunePretrainedPins,
    FineTuneRun,
    FineTuneRuntime,
    FineTuneSampleSet,
    describe_finetune_runtime,
    materialize_finetune_dataset,
    release_finetune_runtime_link,
    run_checkpoints,
    run_f5_finetune,
    run_model_family,
    synthesize_finetune_samples,
)
from .finetune import hyperparameters_family as hyperparameters_family
from .models import F5_FAMILY, ContractModel, StableId
from .paths import AssetLayout
from .recording_contracts import FINETUNE_MAX_EPOCHS


class UnknownTrainingFamily(VoiceLabError):
    """A run or a request names a family this build cannot train."""


# -- progress: F5-TTS's trainer prints tqdm status lines ------------------------------------


@dataclass(frozen=True, slots=True)
class FineTuneTrainingProgress:
    """One trainer status line: pass ``epoch`` of ``epochs``, the update, loss and time left."""

    epoch: int
    epochs: int
    update: int | None
    loss: float | None
    eta_seconds: int | None


_ESCAPES = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SEPARATORS = re.compile(r"[\r\n]")
_EPOCH = re.compile(r"Epoch (\d{1,6})/(\d{1,6})")
# tqdm's elapsed<remaining times: [MM:SS or [H:MM:SS, the leading field at most six digits.
_TIMES = re.compile(r"\[(\d{1,6}(?::\d{2}){1,2})<(\d{1,6}(?::\d{2}){1,2})")
_LOSS = re.compile(r"loss=([^,\]\s]+)")
_UPDATE = re.compile(r"update=(\d{1,18})\b")


def _interval_seconds(text: str) -> int:
    seconds = 0
    for part in text.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def _fragment_progress(fragment: str) -> FineTuneTrainingProgress | None:
    epoch_match = _EPOCH.search(fragment)
    if epoch_match is None:
        return None
    epoch, epochs = int(epoch_match.group(1)), int(epoch_match.group(2))
    if not 1 <= epoch <= epochs <= FINETUNE_MAX_EPOCHS:
        return None
    loss: float | None = None
    loss_match = _LOSS.search(fragment)
    if loss_match is not None:
        try:
            value = float(loss_match.group(1))
        except ValueError:
            value = float("nan")
        loss = value if value == value and abs(value) != float("inf") else None
    update_match = _UPDATE.search(fragment)
    eta: int | None = None
    times = _TIMES.search(fragment)
    if times is not None:
        elapsed, remaining = (_interval_seconds(value) for value in times.groups())
        eta = remaining + (epochs - epoch) * (elapsed + remaining)
    return FineTuneTrainingProgress(
        epoch=epoch,
        epochs=epochs,
        update=None if update_match is None else int(update_match.group(1)),
        loss=loss,
        eta_seconds=eta,
    )


def parse_training_progress(text: str) -> FineTuneTrainingProgress | None:
    """The latest trainer status in ``text``: the last fragment naming ``Epoch E/N``.

    ``text`` is F5-TTS training output: tqdm status lines separated by ``\\r`` (and terminal
    escapes), e.g. ``Epoch 3/120:  89%|███▉ | 8/9 [00:50<00:06, 6.03s/update, loss=0.566,
    update=1079]``. The time left is the current pass's remaining time plus the remaining
    passes at the current pass's length. A non-finite loss is reported as unknown; a pass
    outside ``1..total`` is ignored.
    Returns: the progress, or ``None`` when no fragment names a pass.
    """
    for fragment in reversed(_SEPARATORS.split(_ESCAPES.sub("", text))):
        progress = _fragment_progress(fragment)
        if progress is not None:
            return progress
    return None


# -- the port ----------------------------------------------------------------------------------


class TrainingRuntime(Protocol):
    """A family's described runtime, as the pipeline passes it back to its adapter."""

    @property
    def pretrained_path(self) -> Path: ...


@dataclass(frozen=True, slots=True)
class ListeningSamples:
    """The fine-tuned model's samples and the pretrained base's, from the same texts."""

    finetuned: FineTuneSampleSet
    base: FineTuneSampleSet


class TrainingAdapter(Protocol):
    """What training needs of one model family."""

    @property
    def family_id(self) -> str: ...

    @property
    def model_names(self) -> frozenset[str]:
        """The trainer model names (``hyperparameters.model_name``) this family trains."""
        ...

    @property
    def dataset_sample_rate_hz(self) -> int: ...

    @property
    def pinned_weight_pattern(self) -> str:
        """The name (a glob) of the link to the runtime's pinned pretrained weights that
        training leaves in a run's ``checkpoints/``: the vault records it by its pin and never
        uploads it (``library_classification.PINNED_WEIGHT_FILES`` holds the same)."""
        ...

    def hyperparameters(
        self, options: Mapping[str, object]
    ) -> FineTuneHyperparameters | FamilyHyperparameters:
        """The family's training settings: its defaults with ``options`` (by field name).

        Raises: ``pydantic.ValidationError`` for an option the family does not have or a value
        out of its bounds.
        """
        ...

    def listening_settings(self, options: Mapping[str, object]) -> BaseModel:
        """The family's listening-sample settings: its defaults with ``options``.

        Raises: ``pydantic.ValidationError`` as ``hyperparameters``.
        """
        ...

    def normalise_text(self, text: str) -> str:
        """A prompt's text as the family's dataset holds it.

        Raises: ``FineTuneError`` FINETUNE.UNSUPPORTED_TEXT for a text it cannot hold.
        """
        ...

    def describe_runtime(
        self, runtime_root: Path, *, digests: FineTuneDigestCache
    ) -> TrainingRuntime:
        """Bind the family's isolated runtime: interpreter, packages, pinned weights.

        Raises: ``FineTuneError`` FINETUNE.RUNTIME_UNAVAILABLE or FINETUNE.PRETRAINED_MISMATCH.
        """
        ...

    def runtime_link(self, runtime: TrainingRuntime, run_id: str) -> Path | None:
        """A pointer the runtime keeps into the run while it trains, which whoever stops the
        training releases (``None`` for a family whose runtime keeps none)."""
        ...

    def release_runtime_link(self, link: Path, checkpoints: Path) -> bool: ...

    def build_dataset(
        self, layout: AssetLayout, corpus: FineTuneCorpus, destination: str, *, ffmpeg: Path
    ) -> FineTuneDataset:
        """The family's training copies of ``corpus`` at ``destination``, bound to it."""
        ...

    def train(
        self,
        layout: AssetLayout,
        dataset: FineTuneDataset,
        runtime: TrainingRuntime,
        hyperparameters: FineTuneHyperparameters | FamilyHyperparameters,
        run_root: str,
        *,
        run_id: str,
        subject_id: str,
        catalog_sha256: str,
    ) -> FineTuneRun:
        """Train in the runtime (blocking); write and return ``run.json``.

        Raises: ``FineTuneError`` FINETUNE.TRAINING_FAILED after writing the failed record.
        """
        ...

    def checkpoints(self, layout: AssetLayout, run_root: str) -> tuple[FineTuneCheckpoint, ...]:
        """The checkpoints the run's directory holds, hashed."""
        ...

    def parse_progress(self, text: str) -> FineTuneTrainingProgress | None:
        """The latest progress in a piece of the training log."""
        ...

    def listening_samples(
        self,
        layout: AssetLayout,
        dataset: FineTuneDataset,
        runtime: TrainingRuntime,
        run_root: str,
        *,
        texts: tuple[str, ...],
        settings: BaseModel,
        digests: FineTuneDigestCache,
    ) -> ListeningSamples:
        """The fine-tuned and base samples of ``texts``, with ``settings`` (this family's
        ``listening_settings``). A family with a reference clip chooses it here (F5-TTS: the
        settings' prompt, else the dataset's first clip).

        Raises: ``UnknownTrainingFamily`` for another family's settings; the family's
        ``FineTuneError``.
        """
        ...


def require_trains(
    adapter: TrainingAdapter,
    hyperparameters: FineTuneHyperparameters | FamilyHyperparameters,
) -> None:
    """Refuse ``adapter`` for hyperparameters of another family or model.

    Raises: ``UnknownTrainingFamily``.
    """
    family = hyperparameters_family(hyperparameters)
    if family != adapter.family_id:
        raise UnknownTrainingFamily(
            f"the {adapter.family_id} adapter cannot train {family} hyperparameters"
        )
    if hyperparameters.model_name not in adapter.model_names:
        raise UnknownTrainingFamily(
            f"{adapter.family_id} does not train {hyperparameters.model_name}"
        )


# -- F5-TTS ------------------------------------------------------------------------------------


class F5ListeningSettings(ContractModel):
    """How F5-TTS's listening samples are made: the reference clip's prompt (``None``: the
    dataset's first clip), EMA or raw weights, flow steps, and the device."""

    family: Literal["f5-tts"] = F5_FAMILY
    reference_prompt_id: StableId | None = None
    use_ema: bool = True
    nfe_steps: int = Field(default=32, ge=1, le=256)
    device: F5Device = "mps"


def _f5_runtime(runtime: TrainingRuntime) -> FineTuneRuntime:
    if not isinstance(runtime, FineTuneRuntime):
        raise FineTuneError("FINETUNE.RUNTIME_UNAVAILABLE", "not an F5-TTS runtime")
    return runtime


class F5TrainingAdapter:
    """F5-TTS: ``finetune``'s F5-TTS dataset, runtime, launcher and synthesizer.

    ``pretrained_pin`` substitutes the pretrained-file pins the runtime is checked against
    (a test seam; ``None`` is ``finetune_pretrained_pins()``).
    """

    family_id: Final = F5_FAMILY
    model_names: Final = frozenset({"F5TTS_v1_Base"})
    dataset_sample_rate_hz: Final = FINETUNE_SAMPLE_RATE_HZ
    # ``run_f5_finetune`` hard-links the pinned weights as ``pretrained_<their name>``.
    pinned_weight_pattern: Final = "pretrained_*.safetensors"

    def __init__(self, *, pretrained_pin: FineTunePretrainedPins | None = None) -> None:
        self.pretrained_pin = pretrained_pin

    def hyperparameters(self, options: Mapping[str, object]) -> FineTuneHyperparameters:
        return FineTuneHyperparameters.model_validate(dict(options))

    def listening_settings(self, options: Mapping[str, object]) -> F5ListeningSettings:
        return F5ListeningSettings.model_validate(dict(options))

    def normalise_text(self, text: str) -> str:
        # F5-TTS takes the prompt as written (its tokenizer does the rest); its metadata.csv
        # cannot hold a '|' or a line break.
        if "|" in text or "\n" in text:
            raise FineTuneError(
                "FINETUNE.UNSUPPORTED_TEXT", "prompt text cannot contain '|' or a newline"
            )
        return text

    def describe_runtime(
        self, runtime_root: Path, *, digests: FineTuneDigestCache
    ) -> TrainingRuntime:
        return describe_finetune_runtime(
            runtime_root, pretrained_pin=self.pretrained_pin, digests=digests
        )

    def runtime_link(self, runtime: TrainingRuntime, run_id: str) -> Path | None:
        return _f5_runtime(runtime).checkpoints_directory / run_id

    def release_runtime_link(self, link: Path, checkpoints: Path) -> bool:
        return release_finetune_runtime_link(link, checkpoints)

    def build_dataset(
        self,
        layout: AssetLayout,
        corpus: FineTuneCorpus,
        destination: str,
        *,
        ffmpeg: Path = FINETUNE_FFMPEG,
    ) -> FineTuneDataset:
        return materialize_finetune_dataset(layout, corpus, destination, ffmpeg=ffmpeg)

    def train(
        self,
        layout: AssetLayout,
        dataset: FineTuneDataset,
        runtime: TrainingRuntime,
        hyperparameters: FineTuneHyperparameters | FamilyHyperparameters,
        run_root: str,
        *,
        run_id: str,
        subject_id: str,
        catalog_sha256: str,
    ) -> FineTuneRun:
        if not isinstance(hyperparameters, FineTuneHyperparameters):
            raise UnknownTrainingFamily("F5-TTS trains with FineTuneHyperparameters")
        return run_f5_finetune(
            layout,
            dataset,
            _f5_runtime(runtime),
            hyperparameters,
            run_root,
            run_id=run_id,
            subject_id=subject_id,
            catalog_sha256=catalog_sha256,
        )

    def checkpoints(self, layout: AssetLayout, run_root: str) -> tuple[FineTuneCheckpoint, ...]:
        return run_checkpoints(layout, run_root)

    def parse_progress(self, text: str) -> FineTuneTrainingProgress | None:
        return parse_training_progress(text)

    def listening_samples(
        self,
        layout: AssetLayout,
        dataset: FineTuneDataset,
        runtime: TrainingRuntime,
        run_root: str,
        *,
        texts: tuple[str, ...] = FINETUNE_DEFAULT_SAMPLE_TEXTS,
        settings: BaseModel,
        digests: FineTuneDigestCache,
    ) -> ListeningSamples:
        if not isinstance(settings, F5ListeningSettings):
            raise UnknownTrainingFamily("F5-TTS samples with F5ListeningSettings")
        described = _f5_runtime(runtime)
        reference = settings.reference_prompt_id or dataset.clips[0].prompt_id

        def samples(label: str, checkpoint: Path, ema: bool) -> FineTuneSampleSet:
            return synthesize_finetune_samples(
                layout,
                dataset,
                described,
                checkpoint,
                f"{run_root}/samples/{label}",
                label=label,
                reference_prompt_id=reference,
                texts=texts,
                use_ema=ema,
                nfe_steps=settings.nfe_steps,
                device=settings.device,
                digests=digests,
            )

        finetuned = samples(
            "finetuned", layout.root / run_root / "checkpoints" / "model_last.pt", settings.use_ema
        )
        return ListeningSamples(
            finetuned=finetuned, base=samples("base", described.pretrained_path, True)
        )


F5_TRAINING: Final = F5TrainingAdapter()


# -- the registry ------------------------------------------------------------------------------


def _registry() -> dict[str, TrainingAdapter]:
    return {F5_TRAINING.family_id: F5_TRAINING}


def training_family_ids() -> tuple[str, ...]:
    """The families this build can train."""
    return tuple(sorted(_registry()))


def training_adapter(family_id: str) -> TrainingAdapter:
    """The adapter of ``family_id``.

    Raises: ``UnknownTrainingFamily``.
    """
    adapter = _registry().get(family_id)
    if adapter is None:
        raise UnknownTrainingFamily(f"this build cannot train model family {family_id}")
    return adapter


def adapter_for(
    hyperparameters: FineTuneHyperparameters | FamilyHyperparameters,
) -> TrainingAdapter:
    """The adapter that trains ``hyperparameters``, which must name one of its models.

    Raises: ``UnknownTrainingFamily``.
    """
    adapter = training_adapter(hyperparameters_family(hyperparameters))
    require_trains(adapter, hyperparameters)
    return adapter


def run_family(run: FineTuneRun) -> str:
    """The model family stored run ``run`` trained (every run so far: F5-TTS)."""
    return run_model_family(run)
