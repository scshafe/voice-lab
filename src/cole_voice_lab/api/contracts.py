"""Strict request and response models of ``/api/v1`` (phase 2: meta and intake).

@canonical api.contracts -- every body the API reads or writes is one of these frozen,
extra-forbidding models, serialized as canonical JSON. JSON Schemas for them are generated
into ``schemas/`` by ``cole_voice_lab.schema``. Refusals are ``ApiRefusal`` with a stable
``error`` code; see ``REFUSAL_CODES``.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Final, Literal, get_args

from pydantic import Field, StringConstraints, field_validator, model_validator

from ..capture import MANIFEST_NAME, QC_REPORT_NAME, READY_NAME
from ..intake import MAX_AUDIO_FILE_BYTES, IntakeReceipt
from ..models import ContractModel, Sha256, StableId
from ..paths import validate_relative_path

API_VERSION: Final = "v1"
# A capture package holds its takes plus three control files; the pilot suites have well
# under a thousand prompts.
MAX_SESSION_FILES: Final = 4096
MAX_SESSION_BYTES: Final = 64 * 1024**3
MAX_PATH_LENGTH: Final = 255
MAX_PATH_DEPTH: Final = 4
CONTROL_FILES: Final = frozenset({MANIFEST_NAME, QC_REPORT_NAME, READY_NAME})
# A partial upload is ``<path>.part`` next to where the file will be; no declared path may
# collide with that name.
PART_SUFFIX: Final = ".part"

type ApiErrorCode = Literal[
    "bad_request",
    "unauthorized",
    "forbidden",
    "not_found",
    "method_not_allowed",
    "invalid_json",
    "invalid_request",
    "body_too_large",
    "content_length_required",
    "invalid_content_range",
    "content_range_out_of_bounds",
    "range_not_contiguous",
    "file_not_declared",
    "file_already_received",
    "checksum_mismatch",
    "incomplete",
    "session_not_found",
    "session_conflict",
    "session_busy",
    "session_finalized",
    "session_refused",
    "too_many_open_sessions",
    "insufficient_storage",
    "not_accepted",
    # the journey routes (api/journey_routes.py)
    "journey_unavailable",
    "recording_not_found",
    "unsupported_fields_projection",
    "playback_not_approved",
    "corpus_access_denied",
    "seam_handle_mismatch",
    "audio_not_found",
    "transcript_dir_not_configured",
    "transcript_not_available",
    "transcript_text_missing",
    "invalid_transcript_path",
    "feature_dir_not_configured",
    "feature_detail_missing",
    "invalid_feature_detail_path",
    "invalid_goal",
    "goal_not_found",
    "pair_not_found",
    # the speech routes (speech/routes.py, docs/SPEECH_API.md §6)
    "caller_not_registered",
    "purpose_not_allowed",
    "unsupported_format",
    "callback_not_allowed",
    "idempotency_conflict",
    "voice_release_unavailable",
    "consent_unavailable",
    "gpu_busy",
    "too_many_jobs",
    "daily_budget_exhausted",
    "queue_full",
    "speech_not_found",
    "audio_not_ready",
    "audio_expired",
    "range_not_satisfiable",
    "already_finished",
    "internal_error",
]
REFUSAL_CODES: Final[frozenset[str]] = frozenset(get_args(ApiErrorCode.__value__))


def validate_package_path(value: str) -> str:
    """A declared package member: canonical, relative, not hidden, bounded.

    Returns: ``value`` unchanged.
    Raises: ``ValueError`` for a non-canonical, absolute or parent path, a component that
    starts with ``.``, a path longer than ``MAX_PATH_LENGTH`` or deeper than
    ``MAX_PATH_DEPTH``, or a ``.part`` name, which is reserved for partial uploads.
    """
    validate_relative_path(value)
    parts = PurePosixPath(value).parts
    if len(value) > MAX_PATH_LENGTH or len(parts) > MAX_PATH_DEPTH:
        raise ValueError(f"a package path is at most {MAX_PATH_LENGTH} characters and 4 deep")
    if any(part.startswith(".") for part in parts):
        raise ValueError("a package path has no hidden component")
    if value.endswith(PART_SUFFIX):
        raise ValueError("a package path never ends in .part")
    return value


type PackagePath = Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATH_LENGTH)]


class FileDeclaration(ContractModel):
    """One member of the package the client will upload: its path, digest and size."""

    path: PackagePath
    sha256: Sha256
    size_bytes: int = Field(ge=0, le=MAX_AUDIO_FILE_BYTES)

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return validate_package_path(value)


class IntakeSessionRequest(ContractModel):
    """``POST /api/v1/intake/sessions``: the complete list of the package's files.

    The list names every file of the capture package, the three control files included,
    and nothing else. ``manifest_sha256`` is the digest of ``manifest.json`` and must equal
    that file's declared digest; the intake receipt the publisher writes names it.
    """

    schema_version: Literal["cole-voice-lab.api.intake-session/v1"] = (
        "cole-voice-lab.api.intake-session/v1"
    )
    session_id: StableId
    manifest_sha256: Sha256
    files: tuple[FileDeclaration, ...] = Field(min_length=len(CONTROL_FILES))

    @model_validator(mode="after")
    def _complete(self) -> IntakeSessionRequest:
        paths = [declared.path for declared in self.files]
        if len(paths) > MAX_SESSION_FILES:
            raise ValueError(f"a session declares at most {MAX_SESSION_FILES} files")
        if len(set(paths)) != len(paths):
            raise ValueError("a session declares each path once")
        if paths != sorted(paths):
            raise ValueError("a session declares its files sorted by path")
        missing = CONTROL_FILES - set(paths)
        if missing:
            raise ValueError(f"a session declares its control files; missing {sorted(missing)}")
        directories = {
            parent.as_posix()
            for path in paths
            for parent in PurePosixPath(path).parents
            if parent != PurePosixPath(".")
        }
        if directories & set(paths):
            raise ValueError("a declared file cannot also be a directory of another")
        manifest = next(declared for declared in self.files if declared.path == MANIFEST_NAME)
        if manifest.sha256 != self.manifest_sha256:
            raise ValueError("manifest_sha256 is not the declared digest of manifest.json")
        if sum(declared.size_bytes for declared in self.files) > MAX_SESSION_BYTES:
            raise ValueError(f"a session holds at most {MAX_SESSION_BYTES} bytes")
        return self

    def declared(self, path: str) -> FileDeclaration | None:
        """The declaration of ``path``, or ``None`` when the session does not declare it."""
        return next((declared for declared in self.files if declared.path == path), None)


class FileProgress(ContractModel):
    """How much of one declared file the server holds."""

    path: PackagePath
    size_bytes: int = Field(ge=0)
    received_bytes: int = Field(ge=0)
    complete: bool


type SessionState = Literal["open", "finalized", "accepted", "refused"]


class IntakeSessionStatus(ContractModel):
    """A session as the server sees it.

    ``open``: files are being received (``files`` shows each one's progress). ``finalized``:
    every file was received and checked, and the package waits in ``incoming/`` for the
    publisher. ``accepted``: the publisher published it; ``receipt`` is its intake receipt.
    ``refused``: the publisher refused it; ``refusal`` says why.
    """

    schema_version: Literal["cole-voice-lab.api.intake-session-status/v1"] = (
        "cole-voice-lab.api.intake-session-status/v1"
    )
    session_id: StableId
    manifest_sha256: Sha256
    state: SessionState
    files: tuple[FileProgress, ...]
    receipt: IntakeReceipt | None = None
    refusal: str | None = Field(default=None, max_length=2000)


class ApiMeta(ContractModel):
    """``GET /api/v1/meta``: what this server is and which clients it serves."""

    schema_version: Literal["cole-voice-lab.api.meta/v1"] = "cole-voice-lab.api.meta/v1"
    server_build: str = Field(min_length=1, max_length=200)
    api_versions: tuple[str, ...] = (API_VERSION,)
    minimum_client_version: str = Field(min_length=1, max_length=64)


class ChunkAnswer(ContractModel):
    """``PUT …/files/{path}``: ``partial`` while bytes are missing, ``received`` once the
    file is complete and its digest matched its declaration."""

    state: Literal["partial", "received"]
    received_bytes: int = Field(ge=0)


class ApiRefusal(ContractModel):
    """Every refusal's body: a stable code and a message that carries no secret.

    ``received_bytes`` accompanies refusals of a ``PUT`` (``range_not_contiguous``,
    ``checksum_mismatch``): how much of the file the server holds, so the client resumes
    from there.
    """

    error: ApiErrorCode
    detail: str = Field(max_length=2000)
    received_bytes: int | None = Field(default=None, ge=0)
