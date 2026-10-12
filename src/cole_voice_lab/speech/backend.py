"""The port a render goes through, and the resident F5-TTS backend behind it (D-046).

@canonical speech.backend -- the job runner knows only ``SpeechBackend``: ``prepare`` a
verified voice release (loading it if it is not the one loaded) and get its fingerprint;
``render_segment`` one sentence into a 24 kHz mono 16-bit WAV with a given seed; ``unload``
to give the GPU back. Tests use a fake that writes synthetic audio.

``F5ResidentBackend`` keeps one resident worker (``f5_resident.py`` in the runtime's
interpreter, ``speech.resident``) with the model, the vocoder and the release's checkpoint
and reference clip loaded between jobs. It checks the runtime first through the existing
seam (``finetune.describe_finetune_runtime``: the interpreter, F5-TTS and every pinned
pretrained file), so a PC without its ROCm runtime fails ``runtime_unavailable`` before
anything starts. A changed release restarts the worker; a segment past its deadline kills
it (``render_timeout``).
"""

from __future__ import annotations

import os
import pwd
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Final, Literal, Protocol

from .. import __version__
from ..errors import VoiceLabError
from ..finetune import (
    FineTuneDigestCache,
    FineTuneError,
    FineTunePretrainedPins,
    describe_finetune_runtime,
    synthesis_vocab_path,
)
from ..paths import confined_path
from ..resources import resource_path
from .config import SpeechRuntime
from .contracts import FailureCode, RenderFingerprint
from .release import VerifiedRelease
from .resident import ResidentError, ResidentProcess

RESIDENT_SCRIPT: Final = "f5_resident.py"
RESIDENT_DIRECTORY: Final = "resident"
CACHE_DIRECTORY: Final = "cache"
STARTUP_SECONDS: Final = 300.0

type BackendFailure = FailureCode | Literal["cancelled"]


class SpeechBackendError(VoiceLabError):
    """A render that did not produce audio, with its stable code."""

    def __init__(self, code: BackendFailure, detail: str) -> None:
        super().__init__(detail)
        self.code: BackendFailure = code
        self.detail = detail


class SpeechBackend(Protocol):
    @property
    def loaded(self) -> bool:
        """Whether a model is resident (and so holds GPU memory)."""
        ...

    def prepare(self, release: VerifiedRelease) -> RenderFingerprint:
        """Have ``release`` loaded; return what will speak.

        Raises: ``SpeechBackendError`` (``runtime_unavailable``).
        """
        ...

    def render_segment(
        self, text: str, *, seed: int, output: Path, deadline_seconds: float
    ) -> None:
        """Render ``text`` into ``output`` (24 kHz mono 16-bit WAV) with ``seed``.

        Raises: ``SpeechBackendError`` (``render_failed``, ``render_timeout``).
        """
        ...

    def unload(self) -> None:
        """Stop any resident model and free the GPU."""
        ...


def _environment(runtime: SpeechRuntime, state_root: Path) -> dict[str, str]:
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
        runtime: SpeechRuntime | None,
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
                "no speech runtime is configured on this host (design phase 5, spike S-2)",
            )
        record = release.release
        key = (record.checkpoint_sha256, record.reference_sha256, runtime.device)
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
        root = Path(self.asset_root)
        scratch = self.state_root / RESIDENT_DIRECTORY
        shutil.rmtree(scratch, ignore_errors=True)
        load = {
            "checkpoint_path": str(confined_path(root, record.checkpoint_path, must_exist=True)),
            "vocab_path": str(synthesis_vocab_path(described)),
            "vocoder_path": str(described.vocoder_path),
            "use_ema": True,
            "device": runtime.device,
            "reference_audio_path": str(
                confined_path(root, record.reference_path, must_exist=True)
            ),
            "reference_text": release.resolution.reference.text,
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
            checkpoint_sha256=record.checkpoint_sha256,
            reference_sha256=record.reference_sha256,
            backend="f5-tts-resident",
            runtime={
                "f5_tts_version": str(ready.get("f5_tts_version", identity.f5_tts_version))[:64],
                "torch_version": str(ready.get("torch_version", identity.torch_version))[:64],
                "model_dtype": str(ready.get("model_dtype", "unknown"))[:64],
                "python_sha256": identity.python_sha256,
                "pretrained_sha256": identity.pretrained_sha256,
            },
            device=runtime.device,
            nfe_steps=runtime.nfe_steps,
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

    def unload(self) -> None:
        if self._process is not None:
            self._process.close()
        self._process = None
        self._key = None
        self._fingerprint = None
        shutil.rmtree(self.state_root / RESIDENT_DIRECTORY, ignore_errors=True)
