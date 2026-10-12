"""The port a render goes through (D-046, D-048).

@canonical speech.backend -- the job runner knows only ``SpeechBackend``: ``prepare`` a
verified voice release (loading it if it is not the one loaded) and get its fingerprint;
``render_segment`` one sentence into a 24 kHz mono 16-bit WAV with a given seed; ``unload``
to give the GPU back. Each model family has its backend (F5-TTS's resident worker:
``f5.F5ResidentBackend``); ``families.ReleaseBackend`` hands each release to its family's.
Tests use a fake that writes synthetic audio.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from ..errors import VoiceLabError
from .contracts import FailureCode, RenderFingerprint

if TYPE_CHECKING:
    from .release import VerifiedRelease

type BackendFailure = FailureCode | Literal["cancelled"]


class SpeechBackendError(VoiceLabError):
    """A render that did not produce audio, with its stable code."""

    def __init__(self, code: BackendFailure, detail: str) -> None:
        super().__init__(detail)
        self.code: BackendFailure = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PeakMemory:
    """The most accelerator memory a loaded model used, and how the backend measured it (a
    process's own allocations and a whole card's use are different numbers)."""

    bytes: int
    method: str


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
        """Render ``text`` into ``output`` (24 kHz mono 16-bit WAV) with ``seed`` (a family
        that cannot seed ignores it, and its fingerprint says so).

        Raises: ``SpeechBackendError`` (``render_failed``, ``render_timeout``).
        """
        ...

    def peak_memory(self) -> PeakMemory | None:
        """The most accelerator memory the loaded model has used and how that was measured,
        when the backend can tell (for the model comparison's benchmark,
        docs/MODEL_FAMILIES.md); else ``None``."""
        ...

    def unload(self) -> None:
        """Stop any resident model and free the GPU."""
        ...
