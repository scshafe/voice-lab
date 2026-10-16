"""Model families behind the speech path: the registry and the port each family fills (D-048).

@canonical speech.families -- a model family is one kind of fine-tunable TTS model (F5-TTS
today) with everything the speech path needs to know about it, behind ``ModelFamily``:

- ``designate`` finds, in a finished run, the files a release binds (by role) and the
  family's own section of the release record (``contracts.ReleaseModel``);
- ``verify`` checks a release against the run's records again and gives the render its
  segment limit (the longest text the model takes in one pass) and whatever its backend needs;
- ``base_voice`` is the family's base voice (D-055), what speaks while no release exists: its
  pinned pretrained weights and public reference clip, files of the runtime; ``check_base``
  gives the render its segment limit and backend state for it, as ``verify`` does for a release;
- ``backend`` builds the family's ``SpeechBackend`` from the speech configuration;
- ``native_sample_rate_hz`` and ``seeded`` say what the model renders at and whether it honours
  a seed. Whatever the native rate, a backend delivers the API's one format, 24 kHz mono 16-bit
  (``contracts.API_SAMPLE_RATE_HZ``): it resamples in its own runtime when it must.

The job runner and the routes know only ``SpeechBackend`` and ``release.VerifiedRelease``;
``ReleaseBackend`` is the backend the runner is given, which hands each release to its
family's backend. Adding a family is one runtime lock, one training adapter, one speech
backend and one entry in ``_registry`` (docs/MODEL_FAMILIES.md).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..errors import VoiceLabError
from ..finetune import FINETUNE_OUTPUT_ROOT, FineTuneDigestCache, require_finished_run
from ..paths import AssetLayout
from .backend import PeakMemory, SpeechBackend, SpeechBackendError
from .contracts import RenderFingerprint

if TYPE_CHECKING:
    from .config import SpeechConfig
    from .release import BaseVoice, DesignatedModel, FamilyCheck, VerifiedRelease, VoiceRelease


class UnknownModelFamily(VoiceLabError):
    """A release or a run names a family this build does not have."""


class ModelFamily(Protocol):
    """What the speech path needs of one model family."""

    @property
    def family_id(self) -> str: ...

    @property
    def native_sample_rate_hz(self) -> int: ...

    @property
    def seeded(self) -> bool: ...

    def designate(
        self,
        layout: AssetLayout,
        run_id: str,
        *,
        reference_prompt_id: str | None,
        digests: FineTuneDigestCache,
    ) -> DesignatedModel:
        """The files and the record section a release of finished run ``run_id`` binds.

        Raises: ``VoiceLabError`` (``FineTuneError``…) or ``ValueError`` naming why the run
        cannot be designated.
        """
        ...

    def verify(
        self, layout: AssetLayout, release: VoiceRelease, *, digests: FineTuneDigestCache
    ) -> FamilyCheck:
        """Check ``release`` against its run's records (its files are already re-hashed).

        Raises: ``VoiceLabError`` or ``ValueError`` when it no longer matches.
        """
        ...

    def base_voice(self) -> BaseVoice:
        """The family's base voice: its files (relative to the runtime root) with their pinned
        sizes and digests, and its section of the record."""
        ...

    def check_base(self, voice: BaseVoice, files: Mapping[str, Path]) -> FamilyCheck:
        """What a render of the base voice needs (its files are already hashed; ``files`` are
        their paths by role).

        Raises: ``ValueError`` or ``OSError`` when a file cannot be used.
        """
        ...

    def backend(self, config: SpeechConfig) -> SpeechBackend:
        """The family's backend over the configuration's runtime for it (one that fails
        ``runtime_unavailable`` when the configuration names none)."""
        ...


def _registry() -> dict[str, ModelFamily]:
    from .f5 import F5_TTS

    return {F5_TTS.family_id: F5_TTS}


def family_ids() -> tuple[str, ...]:
    """The families this build can designate and render."""
    return tuple(sorted(_registry()))


def family_for(family_id: str) -> ModelFamily:
    """The family ``family_id``.

    Raises: ``UnknownModelFamily``.
    """
    family = _registry().get(family_id)
    if family is None:
        raise UnknownModelFamily(f"this build has no model family {family_id}")
    return family


def run_family(layout: AssetLayout, run_id: str) -> str:
    """The model family finished run ``run_id`` trained (``training.run_family``).

    Raises: ``FineTuneError`` for a missing, invalid or unfinished run.
    """
    from ..training import run_family as trained_family

    return trained_family(require_finished_run(layout, f"{FINETUNE_OUTPUT_ROOT}/{run_id}", run_id))


class ReleaseBackend:
    """The backend the job runner is given: each release goes to its family's backend.

    One family's backend is held at a time (the GPU slot holds one model); a release of
    another family unloads it first. ``families`` is a test seam.
    """

    def __init__(
        self, config: SpeechConfig, *, families: Callable[[str], ModelFamily] = family_for
    ) -> None:
        self.config = config
        self.families = families
        self._family: str | None = None
        self._backend: SpeechBackend | None = None

    @property
    def loaded(self) -> bool:
        return self._backend is not None and self._backend.loaded

    def prepare(self, release: VerifiedRelease) -> RenderFingerprint:
        family_id = release.release.model_family
        if self._backend is None or self._family != family_id:
            self.unload()
            try:
                family = self.families(family_id)
            except UnknownModelFamily as error:
                raise SpeechBackendError("runtime_unavailable", str(error)) from error
            self._backend = family.backend(self.config)
            self._family = family_id
        return self._backend.prepare(release)

    def render_segment(
        self, text: str, *, seed: int, output: Path, deadline_seconds: float
    ) -> None:
        if self._backend is None:
            raise SpeechBackendError("render_failed", "no release is prepared")
        self._backend.render_segment(
            text, seed=seed, output=output, deadline_seconds=deadline_seconds
        )

    def peak_memory(self) -> PeakMemory | None:
        return None if self._backend is None else self._backend.peak_memory()

    def unload(self) -> None:
        if self._backend is not None:
            self._backend.unload()
