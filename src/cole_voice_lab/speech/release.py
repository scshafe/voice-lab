"""The voice release: the one fine-tuned run of Cole's voice that speaks (D-045, D-048).

@canonical speech.release -- Cole designates a finished fine-tune run of his own voice with
``voice-lab voice release designate``. Each designation is a new, write-once, canonical record
``provenance/voice-releases/<sequence>.json`` (0400) that names the digest of the record
before it, so rotation is append-only and the history stays. The newest is current.

A release is family-neutral (D-048): it names the run's model family, binds the run's
``run.json`` and every file the family needs (``files``: for F5-TTS the checkpoint and the
reference clip) and the family's own section (``model``). It names no consent record:
Cole's standing decision (D-050) is the permission, and the pinned subject and the
designation are the safeguards; a release written before D-050 keeps the consent record's
id and digest it was written with, which nothing reads. The family
(``families.family_for``) says which files a run has and checks them again; this module
does what every family shares. ``verify_release`` re-hashes every file before every job; any
difference refuses the render until Cole designates again. Nothing here reads audio beyond
hashing the files the run already recorded.

Only the pinned subject speaks (``SPEECH_SUBJECT``, the configuration's ``subject_id``): a
designation refuses a run of another subject, and the API and the job runner refuse a release
of another subject (``require_subject``).
"""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, ValidationError, model_validator

from ..canonical import canonical_json_bytes, open_regular_no_follow, write_new_private_file
from ..errors import ContractError, SecurityError, VoiceLabError
from ..finetune import FINETUNE_OUTPUT_ROOT, FineTuneDigestCache, FineTuneError
from ..models import (
    COLE_SUBJECT,
    ContractModel,
    Sha256,
    StableId,
    historical_field,
    reject_explicit_null,
)
from ..paths import AssetLayout, confined_path
from ..recording_contracts import UtcTimestamp
from .contracts import ReleaseFile, ReleaseModel

VOICE_RELEASE_SCHEMA: Final = "cole-voice-lab.voice-release/v1"
RELEASE_DIRECTORY: Final = "voice-releases"
MAX_RELEASE_BYTES: Final = 64 * 1024
# The one subject whose voice speaks (AGENTS.md: never another subject; D-050). The speech
# configuration's ``subject_id`` is pinned to it (``config.SpeechSubject``).
SPEECH_SUBJECT: Final = COLE_SUBJECT


class VoiceReleaseError(VoiceLabError):
    """A release cannot be designated or no longer matches what it names."""

    def __init__(
        self,
        code: Literal["no_release", "release_changed", "not_designatable"],
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
    model_family: StableId
    run_id: StableId
    run_record_sha256: Sha256
    files: tuple[ReleaseFile, ...] = Field(min_length=1, max_length=8)
    model: ReleaseModel
    # Retired with consent records (D-050): only a release designated before names one.
    consent_id: StableId | None = historical_field()
    consent_sha256: Sha256 | None = historical_field()
    blind_review: BlindReviewRef | None = None
    created_at: UtcTimestamp
    designated_by: str = Field(min_length=1, max_length=256)

    @model_validator(mode="after")
    def _chained(self) -> VoiceRelease:
        reject_explicit_null(self, "consent_id", "consent_sha256")
        if (self.consent_id is None) != (self.consent_sha256 is None):
            raise ValueError("a stored consent id and digest are present together")
        if self.release_id != release_id(self.sequence):
            raise ValueError("a release's id is vr- and its four-digit sequence")
        if (self.sequence == 1) != (self.previous_release_sha256 is None):
            raise ValueError("only the first release names no previous release")
        if not self.designated_by.strip():
            raise ValueError("a release names who designated it")
        if self.model.family != self.model_family:
            raise ValueError("a release's model section is its family's")
        roles = [item.role for item in self.files]
        if len(set(roles)) != len(roles):
            raise ValueError("a release names each file role once")
        return self

    def file(self, role: str) -> ReleaseFile:
        """The file of ``role``.

        Raises: ``KeyError`` when the release has none.
        """
        found = next((item for item in self.files if item.role == role), None)
        if found is None:
            raise KeyError(role)
        return found


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
class DesignatedModel:
    """What a family found in a run it would designate: the run's subject, the files the
    release binds and the family's section of the record."""

    subject_id: str
    files: tuple[ReleaseFile, ...]
    model: ReleaseModel


@dataclass(frozen=True, slots=True)
class FamilyCheck:
    """What a family's check of a release gives the render: the longest segment its model
    takes in one pass (UTF-8 bytes), and whatever its backend needs (``state``)."""

    max_segment_bytes: int
    state: object = None


@dataclass(frozen=True, slots=True)
class VerifiedRelease:
    """A release whose files still hash to what it names, with what the render needs:
    each file's path by role, the family's segment limit and the family's own state."""

    release: VoiceRelease
    digest: str
    files: Mapping[str, Path] = field(default_factory=dict)
    max_segment_bytes: int = 1
    family_state: object = None


def run_record_path(layout: AssetLayout, run_id: str) -> Path:
    """``derived/finetune/<run id>/run.json``: where every family's run keeps its record."""
    return confined_path(layout.root, f"{FINETUNE_OUTPUT_ROOT}/{run_id}") / "run.json"


def require_subject(release: VoiceRelease, subject_id: str) -> None:
    """Refuse a release of any subject but ``subject_id`` (never another subject).

    Raises: ``VoiceReleaseError`` ``release_changed``.
    """
    if release.subject_id != subject_id:
        raise VoiceReleaseError(
            "release_changed", f"release {release.release_id} is not the pinned subject's voice"
        )


def _hash_files(
    layout: AssetLayout, files: tuple[ReleaseFile, ...], digests: FineTuneDigestCache
) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for item in files:
        path = confined_path(layout.root, item.path, must_exist=True)
        if digests.sha256(path) != item.sha256:
            raise VoiceReleaseError("release_changed", f"{item.path} changed")
        found[item.role] = path
    return found


def designate(
    layout: AssetLayout,
    *,
    run_id: str,
    designated_by: str,
    now: datetime,
    reference_prompt_id: str | None = None,
    blind_review: BlindReviewRef | None = None,
    digests: FineTuneDigestCache | None = None,
    write: bool = True,
) -> tuple[VoiceRelease, str]:
    """Designate finished run ``run_id`` as the next voice release (``write=False``: only
    build and return the record it would write, for the preview).

    The run's family (``families.run_family``) finds the files it binds; the run must be
    finished, not archived, and of ``SPEECH_SUBJECT`` (the pinned subject; no caller can name
    another); its files must hash to its records. No consent record is read (D-050). The model
    comparison's benchmark renders a run through a preview (``write=False``) without
    designating it.
    Returns: the release (written unless ``write=False``) and its digest.
    Raises: ``VoiceReleaseError`` ``not_designatable`` (naming why) for any of the above;
    ``SecurityError`` for a broken history.
    """
    from .families import family_for, run_family

    cache = FineTuneDigestCache() if digests is None else digests
    try:
        family_id = run_family(layout, run_id)
        family = family_for(family_id)
        designated = family.designate(
            layout, run_id, reference_prompt_id=reference_prompt_id, digests=cache
        )
        run_sha = cache.sha256(run_record_path(layout, run_id))
        _hash_files(layout, designated.files, cache)
    except (VoiceLabError, ValueError, OSError) as error:
        raise VoiceReleaseError("not_designatable", f"run {run_id}: {error}") from error
    if designated.subject_id != SPEECH_SUBJECT:
        raise VoiceReleaseError(
            "not_designatable",
            f"run {run_id} is not a run of the pinned subject {SPEECH_SUBJECT}",
        )
    if blind_review is not None:
        reviews = layout.directory("evaluations") / "blind-reviews"
        if not (reviews / blind_review.review_id).is_dir():
            raise VoiceReleaseError(
                "not_designatable", f"there is no blind review {blind_review.review_id}"
            )
    directory = _directory(layout, create=True)
    with _locked(directory):
        releases = history(layout)
        sequence = len(releases) + 1
        release = VoiceRelease(
            release_id=release_id(sequence),
            sequence=sequence,
            previous_release_sha256=releases[-1][1] if releases else None,
            subject_id=designated.subject_id,
            model_family=family_id,
            run_id=run_id,
            run_record_sha256=run_sha,
            files=designated.files,
            model=designated.model,
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
    """Re-check every file ``release`` names, then its family's own checks; return what a
    render needs.

    Raises: ``VoiceReleaseError`` ``release_changed`` when the family is unknown, or the run,
    a file or the family's records no longer match the release.
    """
    from .families import family_for

    try:
        family = family_for(release.model_family)
        if digests.sha256(run_record_path(layout, release.run_id)) != release.run_record_sha256:
            raise VoiceReleaseError("release_changed", f"run {release.run_id}'s record changed")
        files = _hash_files(layout, release.files, digests)
        checked = family.verify(layout, release, digests=digests)
    except (FineTuneError, VoiceLabError, ValueError, KeyError, OSError) as error:
        raise VoiceReleaseError(
            "release_changed",
            f"release {release.release_id} no longer matches run {release.run_id}; designate "
            f"again ({error})",
        ) from error
    return VerifiedRelease(
        release=release,
        digest=digest,
        files=files,
        max_segment_bytes=checked.max_segment_bytes,
        family_state=checked.state,
    )
