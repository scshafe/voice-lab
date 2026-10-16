"""The voice: the newest voice release, or the base voice while there is none (D-045, D-055).

@canonical speech.release -- each voice release is a new, write-once, canonical record
``provenance/voice-releases/<sequence>.json`` (0400) that names the digest of the record
before it, so rotation is append-only and the history stays. The newest is current: the speech
API always uses the newest voice (D-055). A record is written in two ways:

- **automatically** (``designation`` ``auto``): the job runner, before each job, releases the
  newest finished fine-tune run of the pinned subject that passes its checks, when it finished
  after the newest record was written (``designate_newest``);
- **by Cole** (``designation`` ``owner``): ``voice-lab voice release designate`` pins a run,
  for example an older one to roll back. A pin holds until a run newer than the pin finishes.

While no record exists the **base voice** speaks (``BaseVoice``, id ``base``): the family's
pinned pretrained weights with its public reference clip and that clip's exact transcript (for
F5-TTS, the weights the fine-tune pipeline pins and ``basic_ref_en.wav`` of the runtime's
F5-TTS package). It is built in code, never stored; its digest is that of its canonical bytes,
and its files live in the configured runtime, pinned by size and SHA-256. It is not a voice of
``SPEECH_SUBJECT``: it is the one voice that is not a fine-tuned run of that subject.

A release is family-neutral (D-048): it names the run's model family, binds the run's
``run.json`` and every file the family needs (``files``: for F5-TTS the checkpoint and the
reference clip) and the family's own section (``model``). It names no consent record:
Cole's standing decision (D-050) is the permission, and the pinned subject is the safeguard; a
release written before D-050 keeps the consent record's id and digest it was written with,
which nothing reads. A record written before D-055 (``voice-release/v1``) names no
``designation`` and was Cole's. The family (``families.family_for``) says which files a run
has and checks them again; this module does what every family shares. ``verify_voice``
re-hashes every file before every job; any difference refuses the render. Nothing here reads
audio beyond hashing the files the run already recorded and the base voice's pinned files.

Only the pinned subject's fine-tuned runs speak (``SPEECH_SUBJECT``, the configuration's
``subject_id``): a designation, automatic or Cole's, refuses a run of another subject, and the
API and the job runner refuse a release of another subject (``require_subject``).
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

from pydantic import Field, ValidationError, model_validator

from ..canonical import canonical_json_bytes, open_regular_no_follow, write_new_private_file
from ..errors import ContractError, SecurityError, VoiceLabError
from ..finetune import (
    FINETUNE_OUTPUT_ROOT,
    FineTuneDigestCache,
    FineTuneError,
    finished_finetune_run_ids,
    require_finished_run,
)
from ..models import (
    COLE_SUBJECT,
    ContractModel,
    Sha256,
    StableId,
    historical_field,
    omitted_when_absent,
    reject_explicit_null,
)
from ..paths import AssetLayout, confined_path
from ..recording_contracts import UtcTimestamp
from .contracts import BaseVoiceFile, BaseVoiceModel, ReleaseFile, ReleaseModel

if TYPE_CHECKING:
    from .config import SpeechConfig

# v1: written before D-055, every one Cole's. v2 names its ``designation``.
VOICE_RELEASE_SCHEMA_V1: Final = "cole-voice-lab.voice-release/v1"
VOICE_RELEASE_SCHEMA: Final = "cole-voice-lab.voice-release/v2"
BASE_VOICE_SCHEMA: Final = "cole-voice-lab.base-voice/v1"
# The base voice's id wherever a release id goes (a job's ledger entry names it).
BASE_RELEASE_ID: Final = "base"
# Who designated an automatic release.
AUTO_DESIGNATED_BY: Final = "voice-lab: the newest finished run"
RELEASE_DIRECTORY: Final = "voice-releases"
MAX_RELEASE_BYTES: Final = 64 * 1024
# The one subject whose voice speaks (AGENTS.md: never another subject; D-050). The speech
# configuration's ``subject_id`` is pinned to it (``config.SpeechSubject``).
SPEECH_SUBJECT: Final = COLE_SUBJECT


class VoiceReleaseError(VoiceLabError):
    """A release cannot be designated or no longer matches what it names."""

    def __init__(
        self,
        code: Literal["no_release", "release_changed", "not_designatable", "base_unavailable"],
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


class BlindReviewRef(ContractModel):
    review_id: StableId
    score_sheet_sha256: Sha256


class VoiceRelease(ContractModel):
    """One designation (docs/SPEECH_API.md §3): automatic, or Cole's pin."""

    schema_version: Literal[
        "cole-voice-lab.voice-release/v1", "cole-voice-lab.voice-release/v2"
    ] = VOICE_RELEASE_SCHEMA
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
    # D-055: ``auto`` when the job runner released the newest finished run, ``owner`` for
    # Cole's pin. Every v2 record names it; a v1 record (all Cole's) has none.
    designation: Literal["owner", "auto"] | None = omitted_when_absent()

    @model_validator(mode="after")
    def _chained(self) -> VoiceRelease:
        reject_explicit_null(self, "consent_id", "consent_sha256", "designation")
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
        if (self.schema_version == VOICE_RELEASE_SCHEMA) != (self.designation is not None):
            raise ValueError("a v2 release names its designation, and a v1 release none")
        return self

    def file(self, role: str) -> ReleaseFile:
        """The file of ``role``.

        Raises: ``KeyError`` when the release has none.
        """
        found = next((item for item in self.files if item.role == role), None)
        if found is None:
            raise KeyError(role)
        return found

    @property
    def automatic(self) -> bool:
        """Whether the job runner wrote it (a v1 record was always Cole's)."""
        return self.designation == "auto"


class BaseVoice(ContractModel):
    """The voice while no release exists (D-055): built in code, never stored.

    ``files`` are relative to the configured runtime's root and pinned by size and SHA-256;
    ``model`` is the family's section (for F5-TTS the weights' source and the public reference
    clip's transcript)."""

    schema_version: Literal["cole-voice-lab.base-voice/v1"] = BASE_VOICE_SCHEMA
    release_id: Literal["base"] = BASE_RELEASE_ID
    model_family: StableId
    files: tuple[BaseVoiceFile, ...] = Field(min_length=1, max_length=8)
    model: BaseVoiceModel

    @model_validator(mode="after")
    def _one_family(self) -> BaseVoice:
        if self.model.family != self.model_family:
            raise ValueError("the base voice's model section is its family's")
        roles = [item.role for item in self.files]
        if len(set(roles)) != len(roles):
            raise ValueError("the base voice names each file role once")
        return self

    def file(self, role: str) -> ReleaseFile:
        """The file of ``role``.

        Raises: ``KeyError`` when the base voice has none.
        """
        found = next((item for item in self.files if item.role == role), None)
        if found is None:
            raise KeyError(role)
        return found


# What speaks: the newest release, or the base voice while there is none.
type Voice = VoiceRelease | BaseVoice


def release_id(sequence: int) -> str:
    return f"vr-{sequence:04d}"


def record_bytes(release: VoiceRelease | BaseVoice) -> bytes:
    return canonical_json_bytes(release) + b"\n"


def voice_digest(voice: Voice) -> str:
    """The SHA-256 of ``voice``'s canonical record (a stored release's file holds these bytes)."""
    return hashlib.sha256(record_bytes(voice)).hexdigest()


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
    """The newest release and its digest (``current_voice`` falls back to the base voice).

    Raises: ``VoiceReleaseError`` ``no_release`` when none has been written.
    """
    releases = history(layout)
    if not releases:
        raise VoiceReleaseError("no_release", "no voice release has been designated")
    return releases[-1]


def base_voice(family_id: str) -> BaseVoice:
    """The base voice of model family ``family_id`` (its pins, read at call time).

    Raises: ``UnknownModelFamily``.
    """
    from .families import family_for

    return family_for(family_id).base_voice()


def current_voice(layout: AssetLayout, config: SpeechConfig) -> tuple[Voice, str]:
    """What speaks now and its digest: the newest release, else the base voice of the
    configuration's family (``SpeechConfig.base_family``).

    Raises: ``SecurityError`` or ``ContractError`` for a broken history.
    """
    releases = history(layout)
    if releases:
        return releases[-1]
    voice = base_voice(config.base_family)
    return voice, voice_digest(voice)


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
    """A release (or the base voice) whose files still hash to what it names, with what the
    render needs: each file's path by role, the family's segment limit and the family's own
    state."""

    release: VoiceRelease | BaseVoice
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
    designation: Literal["owner", "auto"] = "owner",
) -> tuple[VoiceRelease, str]:
    """Designate finished run ``run_id`` as the next voice release (``write=False``: only
    build and return the record it would write, for the preview).

    The run's family (``families.run_family``) finds the files it binds; the run must be
    finished, not archived, and of ``SPEECH_SUBJECT`` (the pinned subject; no caller can name
    another); its files must hash to its records. No consent record is read (D-050). The model
    comparison's benchmark renders a run through a preview (``write=False``) without
    designating it. ``designation`` is ``owner`` for Cole's pin and ``auto`` for the job
    runner's (``designate_newest``); an automatic one is written only while the run finished
    after the newest release, checked under the history's lock.
    Returns: the release (written unless ``write=False``) and its digest.
    Raises: ``VoiceReleaseError`` ``not_designatable`` (naming why) for any of the above;
    ``SecurityError`` for a broken history; ``OSError`` when the record cannot be written.
    """
    from .families import family_for, run_family

    cache = FineTuneDigestCache() if digests is None else digests
    try:
        finished_at = require_finished_run(
            layout, f"{FINETUNE_OUTPUT_ROOT}/{run_id}", run_id
        ).finished_at
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
        if (
            designation == "auto"
            and releases
            and (finished_at is None or finished_at <= releases[-1][0].created_at)
        ):
            raise VoiceReleaseError(
                "not_designatable",
                f"run {run_id} finished before release {releases[-1][0].release_id}",
            )
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
            designation=designation,
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


def newer_runs(layout: AssetLayout) -> tuple[str, ...]:
    """The finished runs of a registered family that finished after the newest release was
    written (every one while there is none), newest first: what ``designate_newest`` tries.

    Raises: ``FineTuneError`` for a run record that cannot be read; ``SecurityError`` for a
    broken history.
    """
    from .families import family_ids

    releases = history(layout)
    since = releases[-1][0].created_at if releases else None
    found: list[str] = []
    for run_id in finished_finetune_run_ids(layout, families=frozenset(family_ids())):
        run = require_finished_run(layout, f"{FINETUNE_OUTPUT_ROOT}/{run_id}", run_id)
        assert run.finished_at is not None  # FineTuneRun: finished runs are dated
        if since is not None and run.finished_at <= since:
            break
        found.append(run_id)
    return tuple(found)


def designate_newest(
    layout: AssetLayout,
    *,
    now: datetime,
    digests: FineTuneDigestCache | None = None,
    refused: Callable[[str, str], None] | None = None,
) -> tuple[VoiceRelease, str] | None:
    """Release the newest finished run that passes its checks, when it finished after the
    newest release (D-055: newest wins, with no step of Cole's).

    Each run of ``newer_runs`` is tried, newest first, with ``designate`` (``auto``): the first
    that passes is written; one that fails (another subject, archived, changed) is passed to
    ``refused`` with why, and the next is tried.
    Returns: the release written and its digest, or ``None`` when no run is newer or none passes.
    Raises: ``FineTuneError`` for a run record that cannot be read; ``SecurityError`` for a
    broken history; ``OSError`` when the record cannot be written.
    """
    cache = FineTuneDigestCache() if digests is None else digests
    for run_id in newer_runs(layout):
        try:
            return designate(
                layout,
                run_id=run_id,
                designated_by=AUTO_DESIGNATED_BY,
                now=now,
                digests=cache,
                designation="auto",
            )
        except VoiceReleaseError as error:
            if refused is not None:
                refused(run_id, str(error))
    return None


def _base_runtime_root(config: SpeechConfig, voice: BaseVoice) -> Path:
    runtime = config.runtime_for(voice.model_family)
    if runtime is None:
        raise VoiceReleaseError(
            "base_unavailable",
            f"no {voice.model_family} runtime is configured, and the base voice's files are in it",
        )
    return Path(runtime.runtime_root)


def base_problem(voice: BaseVoice, runtime_root: Path) -> str | None:
    """Why the base voice's files cannot be used, without hashing them, or ``None``: each is
    a regular file (not a link) of its pinned size under ``runtime_root``."""
    for item in voice.files:
        path = runtime_root / item.path
        try:
            found = os.lstat(path)
        except OSError:
            return f"the base voice's {item.role} is missing at {path}"
        if not stat.S_ISREG(found.st_mode):
            return f"the base voice's {item.role} at {path} is not a regular file"
        if found.st_size != item.size_bytes:
            return f"the base voice's {item.role} at {path} is not its pinned size"
    return None


def check_voice(layout: AssetLayout, config: SpeechConfig) -> tuple[Voice, str]:
    """The API's check at submit: what speaks now, usable as far as can be told without
    hashing (a release of the pinned subject; the base voice's files there at their pinned
    sizes). The job runner hashes everything again before each job (``verify_voice``).

    Raises: ``VoiceReleaseError`` ``release_changed`` (another subject) or
    ``base_unavailable``; ``SecurityError`` or ``ContractError`` for a broken history.
    """
    voice, digest = current_voice(layout, config)
    if isinstance(voice, BaseVoice):
        problem = base_problem(voice, _base_runtime_root(config, voice))
        if problem is not None:
            raise VoiceReleaseError("base_unavailable", problem)
    else:
        require_subject(voice, config.subject_id)
    return voice, digest


def verify_base(
    voice: BaseVoice, digest: str, *, runtime_root: Path, digests: FineTuneDigestCache
) -> VerifiedRelease:
    """Hash the base voice's files against their pins, then its family's checks; return what a
    render needs.

    Raises: ``VoiceReleaseError`` ``base_unavailable`` when a file is missing, not a regular
    file or not its pin, or the family refuses it.
    """
    from .families import family_for

    problem = base_problem(voice, runtime_root)
    if problem is not None:
        raise VoiceReleaseError("base_unavailable", problem)
    files: dict[str, Path] = {}
    try:
        for item in voice.files:
            path = runtime_root / item.path
            if digests.sha256(path) != item.sha256:
                raise VoiceReleaseError(
                    "base_unavailable", f"the base voice's {item.role} at {path} is not its pin"
                )
            files[item.role] = path
        checked = family_for(voice.model_family).check_base(voice, files)
    except VoiceReleaseError:
        raise
    except (VoiceLabError, ValueError, KeyError, OSError) as error:
        raise VoiceReleaseError(
            "base_unavailable", f"the base voice cannot be used: {error}"
        ) from error
    return VerifiedRelease(
        release=voice,
        digest=digest,
        files=files,
        max_segment_bytes=checked.max_segment_bytes,
        family_state=checked.state,
    )


def verify_voice(
    layout: AssetLayout,
    voice: Voice,
    digest: str,
    *,
    config: SpeechConfig,
    digests: FineTuneDigestCache,
) -> VerifiedRelease:
    """The job runner's check before each job: a release of the pinned subject re-hashed
    (``verify_release``), or the base voice's files hashed against their pins
    (``verify_base``).

    Raises: ``VoiceReleaseError``.
    """
    if isinstance(voice, BaseVoice):
        return verify_base(
            voice, digest, runtime_root=_base_runtime_root(config, voice), digests=digests
        )
    require_subject(voice, config.subject_id)
    return verify_release(layout, voice, digest, digests=digests)
