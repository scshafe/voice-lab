"""Immutable candidate, runtime, and model-source registry.

The human-readable TOML describes candidate policy.  The JSON lock binds every
executable profile to immutable code/model revisions and an exact curated file
set.  Loading either file is read-only and never resolves a mutable remote name.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from .canonical import sha256_file
from .errors import ContractError
from .models import CandidateSpec, ContractModel, StableId
from .paths import validate_relative_path

ProfileName = Literal["smoke", "quality"]
SourceKind = Literal["git", "huggingface"]

_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_EXACT_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)+(?:[a-z0-9.+-]*)?$")
_PACKAGE_NAME = re.compile(r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")
_ROOT_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)"
    r"(?:\[[A-Za-z0-9,._-]+\])?==(?P<version>[A-Za-z0-9][A-Za-z0-9.+!-]*)$"
)
_ARTIFACT_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)"
    r"==(?P<version>[A-Za-z0-9][A-Za-z0-9.+!-]*) "
    r"--hash=sha256:(?P<sha256>[0-9a-f]{64})$"
)
_GLOB_CHARACTERS = frozenset("*?[")


class CandidateConfiguration(ContractModel):
    schema_version: Literal["cole-voice-lab.candidates/v1"] = "cole-voice-lab.candidates/v1"
    candidates: tuple[CandidateSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_candidates(self) -> CandidateConfiguration:
        identifiers = [candidate.candidate_id for candidate in self.candidates]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("candidate IDs must be unique")
        return self


class SourceFileLock(ContractModel):
    path: str = Field(min_length=1, max_length=1_024)
    size_bytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("path")
    @classmethod
    def _portable_path(cls, value: str) -> str:
        return validate_relative_path(value)


class SourceLock(ContractModel):
    source_id: StableId
    kind: SourceKind
    repository: str = Field(min_length=1, max_length=1_024)
    revision: str = Field(pattern=r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
    license: str = Field(min_length=1, max_length=200)
    allow_patterns: tuple[str, ...] = ()
    files: tuple[SourceFileLock, ...] = ()

    @field_validator("allow_patterns")
    @classmethod
    def _exact_portable_patterns(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("source allow-patterns must be unique")
        for value in values:
            if any(character in value for character in _GLOB_CHARACTERS):
                raise ValueError("source allow-patterns must be exact paths, not globs")
            validate_relative_path(value)
        return values

    @model_validator(mode="after")
    def _source_shape(self) -> SourceLock:
        file_paths = [entry.path for entry in self.files]
        if len(file_paths) != len(set(file_paths)):
            raise ValueError("source file locks must have unique paths")
        if self.kind == "huggingface":
            if not self.allow_patterns:
                raise ValueError("Hugging Face sources require a curated file allow-list")
            if set(file_paths) != set(self.allow_patterns):
                raise ValueError("every allowed Hugging Face file requires size evidence")
        elif self.allow_patterns or self.files:
            raise ValueError("Git source locks do not use Hugging Face file allow-lists")
        return self


class ReviewedBuiltWheelLock(ContractModel):
    """Exact, separately reviewed wheel built because upstream ships no wheel."""

    package: str = Field(pattern=r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+!-]*$")
    source_url: str = Field(pattern=r"^https://files\.pythonhosted\.org/.+$")
    source_filename: str = Field(min_length=1, max_length=255)
    source_size_bytes: int = Field(gt=0)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    wheel_filename: str = Field(min_length=1, max_length=255)
    wheel_size_bytes: int = Field(gt=0)
    wheel_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    build_python: str = Field(pattern=r"^CPython 3\.12\.[0-9]+$")
    setuptools_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)+$")
    wheel_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)+$")
    source_date_epoch: int = Field(ge=315_532_800)
    build_command: tuple[str, ...] = Field(min_length=3)
    build_environment: dict[str, str] = Field(min_length=4)
    compiler: str | None = Field(default=None, min_length=1, max_length=500)
    sdk_version: str | None = Field(default=None, pattern=r"^[0-9]+(?:\.[0-9]+)+$")

    @field_validator("source_filename", "wheel_filename")
    @classmethod
    def _single_filename(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError("reviewed artifact filename must be one path component")
        return value

    @model_validator(mode="after")
    def _reviewed_build_shape(self) -> ReviewedBuiltWheelLock:
        if not self.source_filename.endswith((".tar.gz", ".zip")):
            raise ValueError("reviewed wheel source must be an sdist archive")
        if not self.wheel_filename.endswith(".whl"):
            raise ValueError("reviewed wheel output must be a wheel")
        if self.compiler is None and self.sdk_version is not None:
            raise ValueError("an SDK version requires compiler evidence")
        if self.compiler is not None and self.sdk_version is None:
            raise ValueError("compiled wheels require SDK evidence")
        required_environment = {"LC_ALL", "PYTHONHASHSEED", "SOURCE_DATE_EPOCH", "TZ"}
        if not required_environment.issubset(self.build_environment):
            raise ValueError("reviewed wheel build environment is incomplete")
        if self.build_environment["SOURCE_DATE_EPOCH"] != str(self.source_date_epoch):
            raise ValueError("reviewed wheel build epoch disagrees with its environment")
        return self


class ReviewedBuildToolWheelLock(ContractModel):
    """One exact executable wheel used only to build reviewed local wheels."""

    package: str = Field(pattern=r"^[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*$")
    version: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.+!-]*$")
    source_url: str = Field(pattern=r"^https://files\.pythonhosted\.org/.+$")
    filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("filename")
    @classmethod
    def _wheel_filename(cls, value: str) -> str:
        if value in {".", ".."} or "/" in value or "\\" in value or not value.endswith(".whl"):
            raise ValueError("reviewed build-tool artifact must be one wheel filename")
        return value


class ReviewedBuildToolchainLock(ContractModel):
    """Byte identity for the Python and wheels that produce local artifacts."""

    build_python: str = Field(pattern=r"^CPython 3\.12\.[0-9]+$")
    build_python_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    tool_wheels: tuple[ReviewedBuildToolWheelLock, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_tool_wheels(self) -> ReviewedBuildToolchainLock:
        packages = [_canonical_package(item.package) for item in self.tool_wheels]
        filenames = [item.filename for item in self.tool_wheels]
        if len(packages) != len(set(packages)) or len(filenames) != len(set(filenames)):
            raise ValueError("reviewed build-tool packages and filenames must be unique")
        if set(packages) != {"packaging", "setuptools", "wheel"}:
            raise ValueError("reviewed build toolchain must bind packaging, setuptools, and wheel")
        return self


class RuntimeDependencyPrefixLock(ContractModel):
    """One exact non-system formula tree in the CPython runtime closure."""

    formula: str = Field(pattern=r"^[A-Za-z0-9@+._-]+$")
    prefix_path: str = Field(pattern=r"^/opt/homebrew/Cellar/.+$")
    opt_alias_path: str = Field(pattern=r"^/opt/homebrew/opt/.+$")
    opt_alias_target: str = Field(min_length=1, max_length=1_024)
    tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class PythonRuntimeLock(ContractModel):
    """Host-local byte closure for the base interpreter behind every venv."""

    implementation: Literal["CPython"] = "CPython"
    version: str = Field(pattern=r"^3\.12\.[0-9]+$")
    build: tuple[str, str]
    machine: Literal["arm64"] = "arm64"
    macos_product_version: str = Field(pattern=r"^[0-9]+(?:\.[0-9]+)+$")
    macos_build_version: str = Field(pattern=r"^[0-9A-Za-z]+$")
    prefix_path: str = Field(pattern=r"^/opt/homebrew/Cellar/python@3\.12/.+$")
    opt_alias_path: Literal["/opt/homebrew/opt/python@3.12"] = "/opt/homebrew/opt/python@3.12"
    opt_alias_target: str = Field(min_length=1, max_length=1_024)
    executable_relative_path: Literal["bin/python3.12"] = "bin/python3.12"
    executable_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prefix_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    dependency_prefixes: tuple[RuntimeDependencyPrefixLock, ...] = Field(min_length=5)
    system_library_references: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_closure(self) -> PythonRuntimeLock:
        formulae = [item.formula for item in self.dependency_prefixes]
        prefixes = [item.prefix_path for item in self.dependency_prefixes]
        aliases = [item.opt_alias_path for item in self.dependency_prefixes]
        if (
            len(formulae) != len(set(formulae))
            or len(prefixes) != len(set(prefixes))
            or len(aliases) != len(set(aliases))
        ):
            raise ValueError("Python runtime dependency formulae, prefixes, and aliases are unique")
        if set(formulae) != {"mpdecimal", "openssl@3", "readline", "sqlite", "xz"}:
            raise ValueError(
                "Python runtime closure must bind mpdecimal, openssl@3, readline, sqlite, and xz"
            )
        if tuple(sorted(self.system_library_references)) != self.system_library_references:
            raise ValueError("Python runtime system-library references must be sorted")
        return self


class RuntimeSanitizedFileLock(ContractModel):
    """One reviewed executable startup file removed before runtime imports."""

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    reason: Literal["executable-pth"] = "executable-pth"

    @field_validator("path")
    @classmethod
    def _exact_pth_path(cls, value: str) -> str:
        relative = validate_relative_path(value)
        if not relative.startswith("lib/python3.12/site-packages/") or not relative.endswith(
            ".pth"
        ):
            raise ValueError("runtime sanitizer may remove only an exact site-packages .pth file")
        return relative


class RuntimeLock(ContractModel):
    runtime_id: StableId
    python_version: str = Field(pattern=r"^[0-9]+\.[0-9]+$")
    target_platform: Literal["cpython-3.12-macos-arm64"]
    requirements_path: str = Field(min_length=1, max_length=1_024)
    requirements_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_lock_path: str = Field(min_length=1, max_length=1_024)
    artifact_lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    estimated_install_bytes: int = Field(gt=0)
    packages: dict[str, str] = Field(min_length=1)
    reviewed_built_wheels: tuple[ReviewedBuiltWheelLock, ...] = ()
    reviewed_build_toolchain: ReviewedBuildToolchainLock | None = None
    python_runtime: PythonRuntimeLock
    sanitized_files: tuple[RuntimeSanitizedFileLock, ...] = ()
    code_source_id: StableId

    @field_validator("requirements_path", "artifact_lock_path")
    @classmethod
    def _portable_requirements_path(cls, value: str) -> str:
        return validate_relative_path(value)

    @field_validator("packages")
    @classmethod
    def _exact_package_versions(cls, values: dict[str, str]) -> dict[str, str]:
        if any(not _EXACT_VERSION.fullmatch(version) for version in values.values()):
            raise ValueError("runtime package evidence must use exact versions")
        return values

    @model_validator(mode="after")
    def _runtime_artifact_shape(self) -> RuntimeLock:
        if self.python_version != "3.12":
            raise ValueError("isolated lab workers require CPython 3.12")
        packages = [_canonical_package(item.package) for item in self.reviewed_built_wheels]
        filenames = [item.wheel_filename for item in self.reviewed_built_wheels]
        if len(packages) != len(set(packages)):
            raise ValueError("reviewed built-wheel packages must be unique")
        if len(filenames) != len(set(filenames)):
            raise ValueError("reviewed built-wheel filenames must be unique")
        if bool(self.reviewed_built_wheels) != (self.reviewed_build_toolchain is not None):
            raise ValueError(
                "reviewed local wheels and their hash-locked build toolchain must appear together"
            )
        sanitized_paths = [item.path for item in self.sanitized_files]
        if len(sanitized_paths) != len(set(sanitized_paths)):
            raise ValueError("runtime sanitized file paths must be unique")
        return self


class CandidateProfileLock(ContractModel):
    candidate_id: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
    profile: ProfileName
    model_source_id: StableId
    auxiliary_source_ids: tuple[StableId, ...] = ()
    provenance_source_ids: tuple[StableId, ...] = ()
    settings: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _unique_references(self) -> CandidateProfileLock:
        if len(self.auxiliary_source_ids) != len(set(self.auxiliary_source_ids)):
            raise ValueError("auxiliary source IDs must be unique")
        if len(self.provenance_source_ids) != len(set(self.provenance_source_ids)):
            raise ValueError("provenance source IDs must be unique")
        return self


class ModelSourcesLock(ContractModel):
    schema_version: Literal["cole-voice-lab.model-sources-lock/v1"] = (
        "cole-voice-lab.model-sources-lock/v1"
    )
    resolved_at: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
    sources: tuple[SourceLock, ...] = Field(min_length=1)
    runtimes: tuple[RuntimeLock, ...] = Field(min_length=1)
    profiles: tuple[CandidateProfileLock, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_lock_keys(self) -> ModelSourcesLock:
        source_ids = [source.source_id for source in self.sources]
        runtime_ids = [runtime.runtime_id for runtime in self.runtimes]
        profile_keys = [(profile.candidate_id, profile.profile) for profile in self.profiles]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source IDs must be unique")
        if len(runtime_ids) != len(set(runtime_ids)):
            raise ValueError("runtime IDs must be unique")
        if len(profile_keys) != len(set(profile_keys)):
            raise ValueError("candidate/profile lock keys must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ResolvedCandidate:
    candidate: CandidateSpec
    profile: ProfileName
    runtime: RuntimeLock
    code_source: SourceLock
    model_source: SourceLock
    auxiliary_sources: tuple[SourceLock, ...]
    provenance_sources: tuple[SourceLock, ...]
    settings: dict[str, JsonValue]

    @property
    def model_id(self) -> str:
        return self.model_source.repository

    @property
    def model_revision(self) -> str:
        return self.model_source.revision

    @property
    def code_revision(self) -> str:
        return self.code_source.revision


def _canonical_package(value: str) -> str:
    if not _PACKAGE_NAME.fullmatch(value):
        raise ContractError(f"invalid package name in runtime lock: {value!r}")
    return re.sub(r"[-_.]+", "-", value).lower()


def canonical_package_name(value: str) -> str:
    """Return the canonical package-index spelling after strict validation."""

    return _canonical_package(value)


def _read_runtime_requirement_entries(
    path: Path,
    *,
    artifact_lock: bool,
) -> dict[str, tuple[str, str | None]]:
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ContractError(f"cannot read runtime requirement lock: {path}") from error
    if len(payload.encode("utf-8")) > 2_000_000:
        raise ContractError(f"runtime requirement lock is unexpectedly large: {path}")
    entries: dict[str, tuple[str, str | None]] = {}
    pattern = _ARTIFACT_REQUIREMENT if artifact_lock else _ROOT_REQUIREMENT
    for line_number, raw_line in enumerate(payload.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        matched = pattern.fullmatch(line)
        if matched is None:
            kind = "artifact" if artifact_lock else "root"
            raise ContractError(
                f"runtime {kind} lock contains an unsafe or unpinned line at {path}:{line_number}"
            )
        package = _canonical_package(matched.group("name"))
        if package in entries:
            raise ContractError(f"runtime lock repeats package {package}: {path}")
        digest = matched.groupdict().get("sha256")
        entries[package] = (matched.group("version"), digest)
    if not entries:
        raise ContractError(f"runtime requirement lock is empty: {path}")
    return entries


def verify_runtime_requirements(runtime: RuntimeLock, repository_root: Path) -> None:
    """Verify digests and the one-artifact-per-package fail-closed lock grammar."""

    requirements = repository_root / runtime.requirements_path
    artifact_lock = repository_root / runtime.artifact_lock_path
    for path, expected in (
        (requirements, runtime.requirements_sha256),
        (artifact_lock, runtime.artifact_lock_sha256),
    ):
        digest, _ = sha256_file(path)
        if digest != expected:
            raise ContractError(
                f"requirements digest mismatch for runtime {runtime.runtime_id}: {path}"
            )
    roots = _read_runtime_requirement_entries(requirements, artifact_lock=False)
    artifacts = _read_runtime_requirement_entries(artifact_lock, artifact_lock=True)
    expected_roots = {
        **roots,
        **{
            _canonical_package(package): (version, None)
            for package, version in runtime.packages.items()
        },
    }
    for package, (version, _digest) in expected_roots.items():
        locked = artifacts.get(package)
        if locked is None or locked[0] != version:
            raise ContractError(
                f"runtime artifact lock does not bind root {package}=={version}: {artifact_lock}"
            )
    for reviewed in runtime.reviewed_built_wheels:
        package = _canonical_package(reviewed.package)
        locked = artifacts.get(package)
        if locked != (reviewed.version, reviewed.wheel_sha256):
            raise ContractError(
                f"reviewed wheel evidence disagrees with runtime artifact lock for {package}"
            )


def runtime_artifact_versions(runtime: RuntimeLock, repository_root: Path) -> dict[str, str]:
    """Return the complete verified platform-lock package/version mapping."""

    verify_runtime_requirements(runtime, repository_root)
    artifact_lock = repository_root / runtime.artifact_lock_path
    return {
        package: version
        for package, (version, _digest) in _read_runtime_requirement_entries(
            artifact_lock,
            artifact_lock=True,
        ).items()
    }


class CandidateRegistry:
    """Validated immutable view over candidate policy and source locks."""

    def __init__(self, configuration: CandidateConfiguration, lock: ModelSourcesLock):
        self.configuration = configuration
        self.lock = lock
        self._candidates = {
            candidate.candidate_id: candidate for candidate in configuration.candidates
        }
        self._sources = {source.source_id: source for source in lock.sources}
        self._runtimes = {runtime.runtime_id: runtime for runtime in lock.runtimes}
        self._profiles = {
            (profile.candidate_id, profile.profile): profile for profile in lock.profiles
        }
        self._validate_links()

    def _source(self, source_id: str) -> SourceLock:
        try:
            return self._sources[source_id]
        except KeyError as error:
            raise ContractError(f"unknown locked source: {source_id}") from error

    def _validate_links(self) -> None:
        expected_profiles = {
            (candidate_id, profile)
            for candidate_id in self._candidates
            for profile in ("smoke", "quality")
        }
        actual_profiles = set(self._profiles)
        if expected_profiles != actual_profiles:
            missing = sorted(expected_profiles - actual_profiles)
            extra = sorted(actual_profiles - expected_profiles)
            raise ContractError(
                f"profile locks disagree with candidates; missing={missing}, extra={extra}"
            )

        for candidate in self._candidates.values():
            try:
                runtime = self._runtimes[candidate.runtime_id]
            except KeyError as error:
                raise ContractError(
                    f"candidate {candidate.candidate_id} names unknown runtime "
                    f"{candidate.runtime_id}"
                ) from error
            code_source = self._source(runtime.code_source_id)
            if code_source.kind != "git":
                raise ContractError(f"runtime {runtime.runtime_id} code source must be Git")
            for profile_name in ("smoke", "quality"):
                profile = self._profiles[(candidate.candidate_id, profile_name)]
                model_source = self._source(profile.model_source_id)
                if model_source.kind != candidate.model_source:
                    raise ContractError(
                        f"candidate {candidate.candidate_id} model source kind disagrees"
                    )
                for source_id in (*profile.auxiliary_source_ids, *profile.provenance_source_ids):
                    self._source(source_id)
            quality = self._source(
                self._profiles[(candidate.candidate_id, "quality")].model_source_id
            )
            if quality.repository != candidate.model_id:
                raise ContractError(
                    f"candidate {candidate.candidate_id} model_id must name its quality profile"
                )
            if not candidate.worker_module.startswith("cole_voice_lab.workers."):
                raise ContractError("candidate workers must live under cole_voice_lab.workers")

    def candidate(self, candidate_id: str) -> CandidateSpec:
        try:
            return self._candidates[candidate_id]
        except KeyError as error:
            raise ContractError(f"unknown candidate: {candidate_id}") from error

    def candidates(self, *, track: str | None = None) -> tuple[CandidateSpec, ...]:
        candidates = tuple(sorted(self._candidates.values(), key=lambda item: item.candidate_id))
        if track is None:
            return candidates
        return tuple(candidate for candidate in candidates if candidate.track == track)

    def runtime(self, runtime_id: str) -> RuntimeLock:
        try:
            return self._runtimes[runtime_id]
        except KeyError as error:
            raise ContractError(f"unknown runtime: {runtime_id}") from error

    def source(self, source_id: str) -> SourceLock:
        return self._source(source_id)

    def resolve(self, candidate_id: str, profile: ProfileName = "quality") -> ResolvedCandidate:
        candidate = self.candidate(candidate_id)
        profile_lock = self._profiles[(candidate_id, profile)]
        runtime = self.runtime(candidate.runtime_id)
        settings = dict(candidate.default_settings)
        settings.update(profile_lock.settings)
        return ResolvedCandidate(
            candidate=candidate,
            profile=profile,
            runtime=runtime,
            code_source=self._source(runtime.code_source_id),
            model_source=self._source(profile_lock.model_source_id),
            auxiliary_sources=tuple(
                self._source(source_id) for source_id in profile_lock.auxiliary_source_ids
            ),
            provenance_sources=tuple(
                self._source(source_id) for source_id in profile_lock.provenance_source_ids
            ),
            settings=settings,
        )

    def verify_requirements(self, repository_root: Path) -> None:
        for runtime in self._runtimes.values():
            verify_runtime_requirements(runtime, repository_root)


def load_registry(candidates_path: Path, lock_path: Path) -> CandidateRegistry:
    """Load local registry files without performing network or package operations."""

    try:
        candidates_data = tomllib.loads(candidates_path.read_text(encoding="utf-8"))
        lock_payload = lock_path.read_bytes()
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ContractError(f"cannot load candidate registry: {error}") from error
    raw_candidates = candidates_data.get("candidates")
    if not isinstance(raw_candidates, list):
        raise ContractError("candidate configuration must contain a candidates array")
    configuration = CandidateConfiguration.model_validate(
        {**candidates_data, "candidates": tuple(raw_candidates)}
    )
    lock = ModelSourcesLock.model_validate_json(lock_payload)
    return CandidateRegistry(configuration, lock)


def is_immutable_revision(value: str) -> bool:
    """Return whether a revision is a full lowercase Git or SHA-256 identifier."""

    return _REVISION.fullmatch(value) is not None
