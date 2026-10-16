"""F5-TTS, the first model family of the speech path (D-045, D-046, D-048).

@canonical speech.f5 -- ``F5_TTS`` is F5-TTS's entry in ``families``: a release of an F5-TTS
run binds its ``checkpoint`` (the run's recorded ``model_last.pt``) and its ``reference``
clip (a clip of the run's dataset, chosen as Speak chooses it), resolved through the existing
Speak seam (``finetune.resolve_speech_model``); the segment limit is upstream's per-pass
limit for that reference clip; the model renders 24 kHz mono, the API's own format, and
honours a seed.

Its base voice (D-055) is the pretrained weights the fine-tune pipeline pins
(``finetune.finetune_pretrained_pins``, ``<runtime>/pretrained/F5TTS_v1_Base/``) with F5-TTS's
public English reference clip and its exact transcript, as spike S-2 rendered them (D-046):
``basic_ref_en.wav`` of the F5-TTS 1.1.22 package the hash-locked runtime installs, pinned here
by size and SHA-256 (from the PyPI wheel). Nothing is downloaded for it.

``F5ResidentBackend`` keeps one resident worker (``f5_resident.py`` in the runtime's
interpreter, ``speech.resident``) with the model, the vocoder and the release's checkpoint
and reference clip loaded between jobs. It checks the runtime first through the existing
seam (``finetune.describe_finetune_runtime``: the interpreter, F5-TTS and every pinned
pretrained file), so a PC without its ROCm runtime fails ``runtime_unavailable`` before
anything starts. A changed release restarts the worker; a segment past its deadline kills
it (``render_timeout``).
"""

from __future__ import annotations

import hashlib
import os
import pwd
import shutil
import wave
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .. import __version__
from ..finetune import (
    FINETUNE_PRETRAINED_FILE,
    FINETUNE_PRETRAINED_REPOSITORY,
    FINETUNE_PRETRAINED_REVISION,
    FineTuneDigestCache,
    FineTuneError,
    FineTunePretrainedPins,
    FineTuneRuntimeIdentity,
    SpeechResolution,
    describe_finetune_runtime,
    finetune_pretrained_pins,
    resolve_speech_model,
    synthesis_vocab_path,
)
from ..models import F5_FAMILY
from ..paths import AssetLayout
from ..resources import resource_path
from .backend import PeakMemory, SpeechBackend, SpeechBackendError
from .config import F5SpeechRuntime, SpeechConfig
from .contracts import (
    API_SAMPLE_RATE_HZ,
    BaseVoiceFile,
    F5BaseVoiceModel,
    F5ReleaseModel,
    F5RenderSettings,
    F5RuntimeRef,
    FailureCode,
    FileDigest,
    ReleaseFile,
    RenderFingerprint,
)
from .release import (
    BaseVoice,
    DesignatedModel,
    FamilyCheck,
    VerifiedRelease,
    VoiceRelease,
    VoiceReleaseError,
)
from .resident import ResidentError, ResidentProcess

RESIDENT_SCRIPT: Final = "f5_resident.py"
RESIDENT_DIRECTORY: Final = "resident"
CACHE_DIRECTORY: Final = "cache"
# How the resident worker measures its peak (``f5_resident.py``): torch's own count, on a GPU.
PEAK_MEMORY_METHOD: Final = (
    "torch.cuda.max_memory_allocated in the resident worker: the peak of its own tensors since "
    "it started, the load included; not the caching allocator's reserve, the runtime's own "
    "context, or any other process"
)
STARTUP_SECONDS: Final = 300.0
# F5-TTS renders at 24 kHz, the API's own rate: no resampling.
F5_SAMPLE_RATE_HZ: Final = API_SAMPLE_RATE_HZ
# Upstream's floor for a reference clip so long that its own formula gives almost nothing.
MIN_SEGMENT_BYTES: Final = 50
# The base voice's reference (D-055): F5-TTS 1.1.22's public English example clip, in the
# package the runtime installs from the hash-locked wheel (PyPI f5_tts-1.1.22-py3-none-any.whl,
# SHA-256 f0505dfb…c0a09d1, the lock's own hash). The runtime is a Python 3.12 venv (its lock is
# 3.12's only). The transcript is upstream's (infer/examples/basic/basic.toml).
BASE_REFERENCE_FILE: Final = (
    "lib/python3.12/site-packages/f5_tts/infer/examples/basic/basic_ref_en.wav"
)
BASE_REFERENCE_SHA256: Final = "b0e22048e72414fcc1e6b6342e47a774d748a195ed34e4a5b3fcf416707f2b71"
BASE_REFERENCE_SIZE_BYTES: Final = 256_018
BASE_REFERENCE_TEXT: Final = "Some call me nature, others call me mother nature."
BASE_REFERENCE_SOURCE: Final = "f5-tts 1.1.22: f5_tts/infer/examples/basic/basic_ref_en.wav"


@dataclass(frozen=True, slots=True)
class F5RenderState:
    """What F5-TTS's backend needs beyond the files: the reference clip's exact transcript."""

    reference_text: str


def base_voice() -> BaseVoice:
    """F5-TTS's base voice: the pinned pretrained weights and the public reference clip, with
    their pins read at call time."""
    weights = finetune_pretrained_pins().weights
    return BaseVoice(
        model_family=F5_FAMILY,
        files=(
            BaseVoiceFile(
                role="checkpoint",
                path=f"pretrained/{FINETUNE_PRETRAINED_FILE}",
                sha256=weights.sha256,
                size_bytes=weights.size_bytes,
            ),
            BaseVoiceFile(
                role="reference",
                path=BASE_REFERENCE_FILE,
                sha256=BASE_REFERENCE_SHA256,
                size_bytes=BASE_REFERENCE_SIZE_BYTES,
            ),
        ),
        model=F5BaseVoiceModel(
            weights_repository=FINETUNE_PRETRAINED_REPOSITORY,
            weights_revision=FINETUNE_PRETRAINED_REVISION,
            reference_source=BASE_REFERENCE_SOURCE,
            reference_text=BASE_REFERENCE_TEXT,
        ),
    )


def reference_frames(path: Path) -> int:
    """The frames of a 24 kHz mono 16-bit reference clip.

    Raises: ``ValueError`` for any other shape; ``OSError``.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            shape = (handle.getframerate(), handle.getnchannels(), handle.getsampwidth())
            frames = handle.getnframes()
    except (wave.Error, EOFError) as error:
        raise ValueError(f"{path.name} is not a WAV file: {error}") from error
    if shape != (F5_SAMPLE_RATE_HZ, 1, 2) or frames < 1:
        raise ValueError(f"{path.name} is not 24 kHz mono 16-bit PCM")
    return frames


def max_segment_bytes(reference_text: str, reference_frames: int) -> int:
    """F5-TTS 1.1.22's per-pass text limit for a reference clip (``utils_infer``'s formula:
    the reference text's UTF-8 bytes per second times 22 s less the clip's length)."""
    seconds = max(reference_frames / F5_SAMPLE_RATE_HZ, 0.1)
    limit = int(len(reference_text.encode("utf-8")) / seconds * (22 - seconds))
    return max(limit, MIN_SEGMENT_BYTES)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bound(resolution: SpeechResolution, run_id: str) -> DesignatedModel:
    """What a release of ``resolution``'s run binds."""
    run = resolution.run
    if run is None or run.run_id != run_id:  # pragma: no cover - resolve never returns base here
        raise ValueError(f"run {run_id} did not resolve to itself")
    if not isinstance(run.runtime, FineTuneRuntimeIdentity):
        raise ValueError(f"run {run_id} is not an F5-TTS run")
    recorded = next((item for item in run.checkpoints if item.kind == "last"), None)
    if recorded is None:
        raise ValueError(f"fine-tune run {run_id} records no checkpoint")
    reference = resolution.reference
    return DesignatedModel(
        subject_id=resolution.reference_run.subject_id,
        files=(
            ReleaseFile(role="checkpoint", path=recorded.relative_path, sha256=recorded.sha256),
            ReleaseFile(
                role="reference",
                path=f"{resolution.dataset.dataset_root}/{reference.relative_path}",
                sha256=reference.sha256,
            ),
        ),
        model=F5ReleaseModel(
            reference_prompt_id=reference.prompt_id,
            reference_text_sha256=_sha256_text(reference.text),
            runtime=F5RuntimeRef(
                f5_tts_version=run.runtime.f5_tts_version,
                torch_version=run.runtime.torch_version,
                pretrained_sha256=run.runtime.pretrained_sha256,
            ),
        ),
    )


class F5Family:
    """F5-TTS's entry in the family registry."""

    family_id: Final = F5_FAMILY
    native_sample_rate_hz: Final = F5_SAMPLE_RATE_HZ
    seeded: Final = True

    def designate(
        self,
        layout: AssetLayout,
        run_id: str,
        *,
        reference_prompt_id: str | None,
        digests: FineTuneDigestCache,
    ) -> DesignatedModel:
        return _bound(
            resolve_speech_model(layout, run_id, reference_prompt_id=reference_prompt_id), run_id
        )

    def verify(
        self, layout: AssetLayout, release: VoiceRelease, *, digests: FineTuneDigestCache
    ) -> FamilyCheck:
        model = release.model
        resolution = resolve_speech_model(
            layout, release.run_id, reference_prompt_id=model.reference_prompt_id
        )
        bound = _bound(resolution, release.run_id)
        if (
            {item.role: item for item in bound.files} != {item.role: item for item in release.files}
            or bound.model != model
            or bound.subject_id != release.subject_id
        ):
            raise VoiceReleaseError(
                "release_changed", f"run {release.run_id} no longer records what the release binds"
            )
        reference = resolution.reference
        return FamilyCheck(
            max_segment_bytes=max_segment_bytes(reference.text, reference.frames),
            state=F5RenderState(reference_text=reference.text),
        )

    def base_voice(self) -> BaseVoice:
        return base_voice()

    def check_base(self, voice: BaseVoice, files: Mapping[str, Path]) -> FamilyCheck:
        model = voice.model
        frames = reference_frames(files["reference"])
        return FamilyCheck(
            max_segment_bytes=max_segment_bytes(model.reference_text, frames),
            state=F5RenderState(reference_text=model.reference_text),
        )

    def backend(self, config: SpeechConfig) -> SpeechBackend:
        return F5ResidentBackend(
            config.runtime_for(F5_FAMILY),
            state_root=Path(config.state_root),
            asset_root=Path(config.asset_root),
        )


F5_TTS: Final = F5Family()


def _environment(runtime: F5SpeechRuntime, state_root: Path) -> dict[str, str]:
    """The resident worker's environment: this process's, offline, with writable caches
    under the speech state (the host unit's home and system directories are read-only)."""
    cache = state_root / CACHE_DIRECTORY
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "HF_HUB_OFFLINE": "1",
        "WANDB_MODE": "disabled",
        "XDG_CACHE_HOME": str(cache),
        "MIOPEN_USER_DB_PATH": str(cache / "miopen"),
        "MIOPEN_CUSTOM_CACHE_DIR": str(cache / "miopen"),
    }
    environment.setdefault("HOME", pwd.getpwuid(os.getuid()).pw_dir)
    if runtime.visible_devices is not None:
        environment["HIP_VISIBLE_DEVICES"] = runtime.visible_devices
    return environment


class F5ResidentBackend:
    """The resident F5-TTS backend over one runtime.

    ``python`` and ``resident_script`` are test seams (a stand-in worker under this
    interpreter), as ``pretrained_pin`` is for the runtime check; the shipped runner passes
    none of them.
    """

    def __init__(
        self,
        runtime: F5SpeechRuntime | None,
        *,
        state_root: Path,
        asset_root: Path,
        pretrained_pin: FineTunePretrainedPins | None = None,
        python: str | None = None,
        resident_script: Path | None = None,
        digests: FineTuneDigestCache | None = None,
        startup_seconds: float = STARTUP_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.runtime = runtime
        self.state_root = state_root
        self.asset_root = asset_root
        self.pretrained_pin = pretrained_pin
        self.python = python
        self.resident_script = resident_script
        self.digests = FineTuneDigestCache() if digests is None else digests
        self.startup_seconds = startup_seconds
        self.clock = clock
        self._process: ResidentProcess | None = None
        self._key: tuple[str, str, str] | None = None
        self._fingerprint: RenderFingerprint | None = None

    @property
    def loaded(self) -> bool:
        return self._process is not None and self._process.alive

    def prepare(self, release: VerifiedRelease) -> RenderFingerprint:
        runtime = self.runtime
        if runtime is None:
            raise SpeechBackendError(
                "runtime_unavailable",
                "no F5-TTS runtime is configured on this host (design phase 5, spike S-2)",
            )
        record = release.release
        state = release.family_state
        if record.model_family != F5_FAMILY or not isinstance(state, F5RenderState):
            raise SpeechBackendError("runtime_unavailable", "not an F5-TTS release")
        checkpoint, reference = record.file("checkpoint"), record.file("reference")
        key = (checkpoint.sha256, reference.sha256, runtime.device)
        if self.loaded and self._key == key and self._fingerprint is not None:
            return self._fingerprint
        self.unload()
        try:
            described = describe_finetune_runtime(
                Path(runtime.runtime_root),
                pretrained_pin=self.pretrained_pin,
                digests=self.digests,
            )
        except FineTuneError as error:
            raise SpeechBackendError("runtime_unavailable", str(error)) from error
        scratch = self.state_root / RESIDENT_DIRECTORY
        shutil.rmtree(scratch, ignore_errors=True)
        load = {
            "checkpoint_path": str(release.files["checkpoint"]),
            "vocab_path": str(synthesis_vocab_path(described)),
            "vocoder_path": str(described.vocoder_path),
            "use_ema": True,
            "device": runtime.device,
            "reference_audio_path": str(release.files["reference"]),
            "reference_text": state.reference_text,
            "work_directory": str(scratch),
        }
        python = self.python or str(described.python)
        script = self.resident_script or resource_path(RESIDENT_SCRIPT)
        options = {} if self.clock is None else {"clock": self.clock}
        try:
            self._process = ResidentProcess(
                [python, "-B", str(script)],
                load=load,
                environment=_environment(runtime, self.state_root),
                cwd=self.state_root,
                startup_seconds=self.startup_seconds,
                **options,
            )
        except ResidentError as error:
            shutil.rmtree(scratch, ignore_errors=True)
            raise SpeechBackendError("runtime_unavailable", str(error)) from error
        ready = self._process.ready
        identity = described.identity
        self._key = key
        self._fingerprint = RenderFingerprint(
            model_family=F5_FAMILY,
            backend="f5-tts-resident",
            files=tuple(FileDigest(role=item.role, sha256=item.sha256) for item in record.files),
            runtime={
                "f5_tts_version": str(ready.get("f5_tts_version", identity.f5_tts_version))[:64],
                "torch_version": str(ready.get("torch_version", identity.torch_version))[:64],
                "model_dtype": str(ready.get("model_dtype", "unknown"))[:64],
                "python_sha256": identity.python_sha256,
                "pretrained_sha256": identity.pretrained_sha256,
            },
            device=runtime.device,
            native_sample_rate_hz=F5_SAMPLE_RATE_HZ,
            seeded=True,
            settings=F5RenderSettings(nfe_steps=runtime.nfe_steps),
            voice_lab_version=__version__,
        )
        return self._fingerprint

    def render_segment(
        self, text: str, *, seed: int, output: Path, deadline_seconds: float
    ) -> None:
        process, runtime = self._process, self.runtime
        if process is None or runtime is None or not process.alive:
            raise SpeechBackendError("render_failed", "no resident worker is loaded")
        try:
            process.render(
                text,
                seed=seed,
                nfe_steps=runtime.nfe_steps,
                output=output,
                deadline_seconds=deadline_seconds,
            )
        except ResidentError as error:
            if not error.segment_only:
                self.unload()
            code: FailureCode = "render_timeout" if error.timed_out else "render_failed"
            raise SpeechBackendError(code, str(error)) from error

    def peak_memory(self) -> PeakMemory | None:
        peak = None if self._process is None else self._process.peak_memory_bytes
        return None if peak is None else PeakMemory(bytes=peak, method=PEAK_MEMORY_METHOD)

    def unload(self) -> None:
        if self._process is not None:
            self._process.close()
        self._process = None
        self._key = None
        self._fingerprint = None
        shutil.rmtree(self.state_root / RESIDENT_DIRECTORY, ignore_errors=True)
