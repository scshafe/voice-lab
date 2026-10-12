"""The voice release: the one fine-tuned run of Cole's voice that speaks (D-045).

@canonical speech.release -- Cole designates a finished F5-TTS fine-tune run of his own
voice with ``voice-lab voice release designate``. Each designation is a new, write-once,
canonical record ``provenance/voice-releases/<sequence>.json`` (0400) that names the digest
of the record before it, so rotation is append-only and the history stays. The newest is
current.

A release binds the run's ``run.json``, the checkpoint the run recorded, the reference clip
in the run's dataset, the consent record at designation and the run's runtime identity, all
by SHA-256. ``verify_release`` re-checks them through the existing Speak seam
(``finetune.resolve_speech_model``) before every render; any difference refuses the render
until Cole designates again. Nothing here reads audio beyond hashing the files the run
already recorded.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, TypeAdapter, ValidationError, model_validator

from ..canonical import canonical_json_bytes, open_regular_no_follow, write_new_private_file
from ..consent import load_valid_consent
from ..errors import ContractError, SecurityError, VoiceLabError
from ..finetune import (
    FINETUNE_OUTPUT_ROOT,
    FineTuneDigestCache,
    FineTuneError,
    SpeechResolution,
    resolve_speech_model,
)
from ..models import ContractModel, Sha256, StableId
from ..paths import AssetLayout, confined_path
from ..recording_contracts import UtcTimestamp

VOICE_RELEASE_SCHEMA: Final = "cole-voice-lab.voice-release/v1"
RELEASE_DIRECTORY: Final = "voice-releases"
RELEASE_PURPOSE: Final = "private-assistant-speech"
RELEASE_BOUNDARY: Final = "home-host"
MAX_RELEASE_BYTES: Final = 64 * 1024


class VoiceReleaseError(VoiceLabError):
    """A release cannot be designated or no longer matches what it names."""

    def __init__(
        self,
        code: Literal["no_release", "release_changed", "consent_changed", "not_designatable"],
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


class BlindReviewRef(ContractModel):
    review_id: StableId
    score_sheet_sha256: Sha256


class VoiceRelease(ContractModel):
    """One designation (docs/SPEECH_API.md §3)."""

    schema_version: Literal["cole-voice-lab.voice-release/v1"] = VOICE_RELEASE_SCHEMA
    release_id: StableId
    sequence: int = Field(ge=1, le=9999)
    previous_release_sha256: Sha256 | None
    subject_id: StableId
    run_id: StableId
    run_record_sha256: Sha256
    checkpoint_path: str = Field(min_length=1, max_length=4096)
    checkpoint_sha256: Sha256
    reference_prompt_id: StableId
    reference_path: str = Field(min_length=1, max_length=4096)
    reference_sha256: Sha256
    reference_text_sha256: Sha256
    consent_id: StableId
    consent_sha256: Sha256
    runtime: dict[str, str] = Field(max_length=16)
    blind_review: BlindReviewRef | None = None
    created_at: UtcTimestamp
    designated_by: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _chained(self) -> VoiceRelease:
        if self.release_id != release_id(self.sequence):
            raise ValueError("a release's id is vr- and its four-digit sequence")
        if (self.sequence == 1) != (self.previous_release_sha256 is None):
            raise ValueError("only the first release names no previous release")
        if not self.designated_by.strip():
            raise ValueError("a release names who designated it")
        return self


_STABLE_ID: TypeAdapter[StableId] = TypeAdapter(StableId)


def consent_path(layout: AssetLayout, consent_id: str) -> Path:
    """The consent record's path, computed without touching the directory (the API reads the
    asset root through a read-only mount, where ``consent.consent_record_path`` would try to
    fix the directory's mode)."""
    validated = _STABLE_ID.validate_python(consent_id, strict=True)
    return layout.directory("provenance") / "consents" / f"{validated}.json"


def release_id(sequence: int) -> str:
    return f"vr-{sequence:04d}"


def record_bytes(release: VoiceRelease) -> bytes:
    return canonical_json_bytes(release) + b"\n"


def _directory(layout: AssetLayout, *, create: bool) -> Path:
    directory = layout.directory("provenance") / RELEASE_DIRECTORY
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink():
        raise SecurityError(f"the voice-release directory is a link: {directory}")
    return directory


def _read(path: Path) -> tuple[VoiceRelease, str]:
    with open_regular_no_follow(path) as stream:
        data = stream.read(MAX_RELEASE_BYTES + 1)
    try:
        release = VoiceRelease.model_validate_json(data)
    except ValidationError as error:
        raise ContractError(f"invalid voice release {path.name}: {error}") from error
    if record_bytes(release) != data:
        raise SecurityError(f"voice release {path.name} is not in canonical form")
    return release, hashlib.sha256(data).hexdigest()


def history(layout: AssetLayout) -> list[tuple[VoiceRelease, str]]:
    """Every release with its digest, oldest first; the chain is checked as it is read.

    Raises: ``SecurityError`` for a gap, a stray file or a broken link.
    """
    directory = _directory(layout, create=False)
    if not directory.exists():
        return []
    found: list[tuple[VoiceRelease, str]] = []
    names = sorted(name for name in os.listdir(directory) if not name.startswith("."))
    for index, name in enumerate(names, start=1):
        if name != f"{index:04d}.json":
            raise SecurityError(f"the voice-release history has a gap or a stray file: {name}")
        release, digest = _read(directory / name)
        previous = found[-1][1] if found else None
        if release.sequence != index or release.previous_release_sha256 != previous:
            raise SecurityError(f"the voice-release chain is broken at {name}")
        found.append((release, digest))
    return found


def current_release(layout: AssetLayout) -> tuple[VoiceRelease, str]:
    """The newest release and its digest.

    Raises: ``VoiceReleaseError`` ``no_release`` when Cole has designated none.
    """
    releases = history(layout)
    if not releases:
        raise VoiceReleaseError("no_release", "no voice release has been designated")
    return releases[-1]


@contextmanager
def _locked(directory: Path) -> Iterator[None]:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    """A release whose files still hash to what it names, with what the render needs."""

    release: VoiceRelease
    digest: str
    resolution: SpeechResolution


def _run_root(run_id: str) -> str:
    return f"{FINETUNE_OUTPUT_ROOT}/{run_id}"


def _check_files(
    layout: AssetLayout,
    run_id: str,
    reference_prompt_id: str | None,
    digests: FineTuneDigestCache,
) -> tuple[SpeechResolution, str, str, str, str]:
    """Resolve ``run_id`` as Speak does and hash what it names.

    Returns: the resolution, the run record's digest, the checkpoint's relative path and
    digest, and the reference clip's relative path.
    Raises: ``FineTuneError`` from the resolution; ``VoiceReleaseError`` ``release_changed``
    when the checkpoint or the reference clip no longer hashes to the run's record.
    """
    resolution = resolve_speech_model(layout, run_id, reference_prompt_id=reference_prompt_id)
    run = resolution.run
    if run is None or run.run_id != run_id:  # pragma: no cover - resolve never returns base here
        raise VoiceReleaseError("release_changed", f"run {run_id} did not resolve to itself")
    run_root = _run_root(run_id)
    run_record_sha256 = digests.sha256(confined_path(layout.root, run_root) / "run.json")
    recorded = next((item for item in run.checkpoints if item.kind == "last"), None)
    if recorded is None:
        raise VoiceReleaseError("release_changed", f"fine-tune run {run_id} records no checkpoint")
    checkpoint = confined_path(layout.root, recorded.relative_path, must_exist=True)
    if digests.sha256(checkpoint) != recorded.sha256:
        raise VoiceReleaseError(
            "release_changed", f"{recorded.relative_path} changed since run {run_id} finished"
        )
    reference = resolution.reference
    reference_path = f"{resolution.dataset.dataset_root}/{reference.relative_path}"
    if digests.sha256(confined_path(layout.root, reference_path, must_exist=True)) != (
        reference.sha256
    ):
        raise VoiceReleaseError(
            "release_changed", f"the reference clip {reference.prompt_id} changed since packing"
        )
    return resolution, run_record_sha256, recorded.relative_path, recorded.sha256, reference_path


def designate(
    layout: AssetLayout,
    *,
    run_id: str,
    designated_by: str,
    now: datetime,
    reference_prompt_id: str | None = None,
    consent_id: str = "cole-self-v1",
    blind_review: BlindReviewRef | None = None,
    digests: FineTuneDigestCache | None = None,
    write: bool = True,
) -> tuple[VoiceRelease, str]:
    """Designate finished run ``run_id`` as the next voice release (``write=False``: only
    build and return the record it would write, for the preview).

    The run must be finished and not archived; its checkpoint and reference clip must hash
    to its records; an approved consent record for ``private-assistant-speech`` on
    ``home-host`` must name the run's subject.
    Returns: the release written and its digest.
    Raises: ``VoiceReleaseError`` ``not_designatable`` (naming why) for any of the above;
    ``SecurityError`` for a broken history.
    """
    cache = FineTuneDigestCache() if digests is None else digests
    try:
        resolution, run_sha, checkpoint_path, checkpoint_sha, reference_path = _check_files(
            layout, run_id, reference_prompt_id, cache
        )
    except (FineTuneError, VoiceReleaseError, ValueError) as error:
        raise VoiceReleaseError("not_designatable", f"run {run_id}: {error}") from error
    run = resolution.run
    assert run is not None
    record_path = consent_path(layout, consent_id)
    try:
        with open_regular_no_follow(record_path) as stream:
            consent_sha256 = hashlib.sha256(stream.read()).hexdigest()
        consent = load_valid_consent(
            record_path,
            expected_sha256=consent_sha256,
            subject_id=run.subject_id,
            purpose=RELEASE_PURPOSE,
            execution_boundary=RELEASE_BOUNDARY,
            at=now,
        )
    except (SecurityError, ContractError, ValidationError, OSError) as error:
        raise VoiceReleaseError(
            "not_designatable", f"no approved consent record allows it: {error}"
        ) from error
    if blind_review is not None:
        reviews = layout.directory("evaluations") / "blind-reviews"
        if not (reviews / blind_review.review_id).is_dir():
            raise VoiceReleaseError(
                "not_designatable", f"there is no blind review {blind_review.review_id}"
            )
    runtime = run.runtime
    directory = _directory(layout, create=True)
    with _locked(directory):
        releases = history(layout)
        sequence = len(releases) + 1
        release = VoiceRelease(
            release_id=release_id(sequence),
            sequence=sequence,
            previous_release_sha256=releases[-1][1] if releases else None,
            subject_id=consent.subject_id,
            run_id=run.run_id,
            run_record_sha256=run_sha,
            checkpoint_path=checkpoint_path,
            checkpoint_sha256=checkpoint_sha,
            reference_prompt_id=resolution.reference.prompt_id,
            reference_path=reference_path,
            reference_sha256=resolution.reference.sha256,
            reference_text_sha256=hashlib.sha256(
                resolution.reference.text.encode("utf-8")
            ).hexdigest(),
            consent_id=consent.consent_id,
            consent_sha256=consent_sha256,
            runtime={
                "f5_tts_version": runtime.f5_tts_version,
                "torch_version": runtime.torch_version,
                "pretrained_sha256": runtime.pretrained_sha256,
            },
            blind_review=blind_review,
            created_at=now,
            designated_by=designated_by,
        )
        data = record_bytes(release)
        if write:
            write_new_private_file(directory / f"{sequence:04d}.json", data, mode=0o400)
    return release, hashlib.sha256(data).hexdigest()


def verify_release(
    layout: AssetLayout,
    release: VoiceRelease,
    digest: str,
    *,
    digests: FineTuneDigestCache,
) -> VerifiedRelease:
    """Re-check every file ``release`` names; return what a render needs.

    Raises: ``VoiceReleaseError`` ``release_changed`` when the run, its checkpoint or its
    reference clip no longer matches the release.
    """
    try:
        resolution, run_sha, checkpoint_path, checkpoint_sha, reference_path = _check_files(
            layout, release.run_id, release.reference_prompt_id, digests
        )
    except (FineTuneError, ValueError, SecurityError, OSError) as error:
        raise VoiceReleaseError("release_changed", f"release {release.release_id}: {error}") from (
            error
        )
    if (
        run_sha != release.run_record_sha256
        or (checkpoint_path, checkpoint_sha) != (release.checkpoint_path, release.checkpoint_sha256)
        or (reference_path, resolution.reference.sha256)
        != (release.reference_path, release.reference_sha256)
        or resolution.reference_run.subject_id != release.subject_id
    ):
        raise VoiceReleaseError(
            "release_changed",
            f"release {release.release_id} no longer matches run {release.run_id}; designate again",
        )
    return VerifiedRelease(release=release, digest=digest, resolution=resolution)


def require_release_consent(layout: AssetLayout, release: VoiceRelease, *, at: datetime) -> None:
    """Check that the consent record the release names is still the same, valid record.

    Raises: ``VoiceReleaseError`` ``consent_changed`` when it changed, was revoked, expired,
    or no longer allows ``private-assistant-speech`` on ``home-host``.
    """
    try:
        load_valid_consent(
            consent_path(layout, release.consent_id),
            expected_sha256=release.consent_sha256,
            subject_id=release.subject_id,
            purpose=RELEASE_PURPOSE,
            execution_boundary=RELEASE_BOUNDARY,
            at=at,
        )
    except (SecurityError, ContractError, ValidationError, OSError) as error:
        raise VoiceReleaseError("consent_changed", f"consent: {error}") from error
