"""Shared fail-closed JSON worker protocol and evidence capture."""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
import sys
import time
import traceback
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, TextIO, cast

from pydantic import JsonValue

from ..audio import inspect_pcm_wav
from ..canonical import canonical_json_bytes, canonical_sha256, sha256_file, write_canonical_json
from ..errors import (
    ContractError,
    RuntimeUnavailableError,
    SecurityError,
    VoiceLabError,
)
from ..models import (
    EnvironmentEvidence,
    ExperimentRequest,
    ExperimentResult,
    require_cole_subject,
)
from ..runtime import (
    SOURCE_MANIFEST_NAME,
    LocalSourceManifest,
    verify_git_checkout_integrity,
)

_MAX_REQUEST_BYTES = 2 * 1024 * 1024
_COMMON_SETTINGS = frozenset({"profile", "timing_mode", "timing_semantics", "prompt_id"})

LoadFunction = Callable[[ExperimentRequest, "WorkerContext"], object]
SynthesizeFunction = Callable[[object, ExperimentRequest, "WorkerContext", Path | None], int | None]
SynchronizeFunction = Callable[[], None]


def integer_setting(
    request: ExperimentRequest, name: str, default: int, *, minimum: int | None = None
) -> int:
    value = request.settings.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"setting {name} must be an integer")
    if minimum is not None and value < minimum:
        raise ContractError(f"setting {name} must be at least {minimum}")
    return value


def float_setting(request: ExperimentRequest, name: str, default: float) -> float:
    value = request.settings.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError(f"setting {name} must be numeric")
    return float(value)


def bool_setting(request: ExperimentRequest, name: str, default: bool) -> bool:
    value = request.settings.get(name, default)
    if not isinstance(value, bool):
        raise ContractError(f"setting {name} must be boolean")
    return value


@dataclass(frozen=True, slots=True)
class WorkerDefinition:
    candidate_id: str
    track: Literal["direct-tts", "voice-conversion"]
    code_revision: str
    model_revisions: Mapping[str, str]
    auxiliary_revisions: Mapping[str, str]
    allowed_settings: frozenset[str]
    package_name: str | None
    package_version: str | None
    watermarked: bool | None = None


@dataclass(frozen=True, slots=True)
class WorkerContext:
    model_path: Path
    auxiliary_paths: Mapping[str, Path]
    code_path: Path | None
    model_manifest_sha256: str
    auxiliary_manifest_sha256: Mapping[str, str]
    code_checkout_integrity_sha256: str | None = None

    @classmethod
    def from_environment(
        cls, definition: WorkerDefinition, request: ExperimentRequest
    ) -> WorkerContext:
        raw_model_path = os.environ.get("COLE_VOICE_LAB_MODEL_PATH")
        if not raw_model_path:
            raise RuntimeUnavailableError("worker requires a provisioned local model path")
        model_path = Path(raw_model_path).expanduser().resolve(strict=True)
        model_digest = _verify_source_manifest(
            model_path,
            repository=request.model_id,
            revision=request.model_revision,
        )
        try:
            raw_auxiliary = json.loads(os.environ.get("COLE_VOICE_LAB_AUXILIARY_PATHS", "{}"))
        except json.JSONDecodeError as error:
            raise ContractError("invalid auxiliary path map") from error
        if not isinstance(raw_auxiliary, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_auxiliary.items()
        ):
            raise ContractError("auxiliary path map must contain string paths")
        if set(raw_auxiliary) != set(definition.auxiliary_revisions):
            raise ContractError("worker auxiliary path set does not match its immutable definition")
        auxiliary_paths: dict[str, Path] = {}
        auxiliary_digests: dict[str, str] = {}
        for repository, revision in definition.auxiliary_revisions.items():
            path = Path(raw_auxiliary[repository]).expanduser().resolve(strict=True)
            auxiliary_paths[repository] = path
            auxiliary_digests[repository] = _verify_source_manifest(
                path,
                repository=repository,
                revision=revision,
            )
        raw_code_path = os.environ.get("COLE_VOICE_LAB_CODE_PATH", "")
        code_path = Path(raw_code_path).expanduser().resolve(strict=True) if raw_code_path else None
        expected_code_integrity = os.environ.get("COLE_VOICE_LAB_CODE_INTEGRITY_SHA256", "")
        code_integrity: str | None = None
        if code_path is not None:
            if len(expected_code_integrity) != 64 or any(
                character not in "0123456789abcdef" for character in expected_code_integrity
            ):
                raise ContractError("worker requires parent-verified code checkout integrity")
            code_integrity, _files = verify_git_checkout_integrity(
                code_path, definition.code_revision
            )
            if code_integrity != expected_code_integrity:
                raise SecurityError("worker code checkout differs from parent verification")
        elif expected_code_integrity:
            raise ContractError("code checkout integrity was supplied without a checkout")
        return cls(
            model_path=model_path,
            auxiliary_paths=auxiliary_paths,
            code_path=code_path,
            model_manifest_sha256=model_digest,
            auxiliary_manifest_sha256=auxiliary_digests,
            code_checkout_integrity_sha256=code_integrity,
        )


def _verify_source_manifest(path: Path, *, repository: str, revision: str) -> str:
    manifest_path = path / SOURCE_MANIFEST_NAME
    digest, _ = sha256_file(manifest_path)
    manifest = LocalSourceManifest.model_validate_json(manifest_path.read_bytes())
    if (
        manifest.kind != "huggingface"
        or manifest.repository != repository
        or manifest.revision != revision
    ):
        raise ContractError(f"local model manifest does not bind {repository}@{revision}")
    return digest


def _validate_request(definition: WorkerDefinition, request: ExperimentRequest) -> None:
    if request.candidate_id != definition.candidate_id or request.track != definition.track:
        raise ContractError("request candidate/track does not match worker")
    expected_revision = definition.model_revisions.get(request.model_id)
    if expected_revision is None or expected_revision != request.model_revision:
        raise ContractError("request model is not an immutable revision supported by worker")
    if request.code_revision != definition.code_revision:
        raise ContractError("request code revision does not match worker")
    unknown = set(request.settings) - definition.allowed_settings - _COMMON_SETTINGS
    if unknown:
        raise ContractError(f"unsupported worker settings: {sorted(unknown)}")
    timing_mode = request.settings.get("timing_mode", "cold")
    if timing_mode not in {"cold", "warm"}:
        raise ContractError("timing_mode must be cold or warm")
    if definition.track == "voice-conversion" and timing_mode != "cold":
        raise ContractError("voice conversion is a separate cold-only timing track")


def _verify_human_bindings(request: ExperimentRequest) -> tuple[str, str | None, int | None]:
    reference_path = Path(request.reference.path)
    reference_digest, _ = sha256_file(reference_path)
    if reference_digest != request.reference.sha256:
        raise SecurityError("reference audio digest does not match request evidence")
    # Cole's voice only (D-050); no consent record is read, and one a request written before
    # names is ignored.
    require_cole_subject(request.reference.subject_id, what="the request's reference")
    source_digest: str | None = None
    source_size: int | None = None
    if request.source_audio_path is not None:
        source_digest, source_size = sha256_file(Path(request.source_audio_path))
        if (
            source_digest != request.source_audio_sha256
            or source_size != request.source_audio_size_bytes
        ):
            raise SecurityError(
                "performed source audio digest or size does not match request evidence"
            )
    return reference_digest, source_digest, source_size


def _write_exclusive(path: Path, payload: bytes) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _environment_evidence(output_path: Path, trial_id: str) -> EnvironmentEvidence:
    completed = subprocess.run(
        (sys.executable, "-m", "pip", "freeze", "--all"),
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeUnavailableError(f"cannot capture pip evidence: {completed.stderr.strip()}")
    lines = sorted(line.strip() for line in completed.stdout.splitlines() if line.strip())
    payload = (("\n".join(lines) + "\n") if lines else "").encode()
    path = output_path.parent / "evidence" / f"{trial_id}.pip-freeze.txt"
    digest = _write_exclusive(path, payload)
    return EnvironmentEvidence(
        python_version=platform.python_version(),
        platform=platform.platform(),
        machine=platform.machine(),
        dependency_fingerprint_sha256=digest,
        dependency_evidence_path=f"evidence/{trial_id}.pip-freeze.txt",
    )


def _installed_package_version(definition: WorkerDefinition) -> str | None:
    if definition.package_name is None:
        return None
    try:
        version = importlib.metadata.version(definition.package_name)
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeUnavailableError(
            f"required runtime package is missing: {definition.package_name}"
        ) from error
    if definition.package_version is not None and version != definition.package_version:
        raise RuntimeUnavailableError(f"runtime package drift: {definition.package_name} {version}")
    return version


def _distribution_file_evidence(package_name: str) -> dict[str, JsonValue]:
    try:
        distribution = importlib.metadata.distribution(package_name)
    except importlib.metadata.PackageNotFoundError as error:
        raise RuntimeUnavailableError(
            f"required executable distribution is missing: {package_name}"
        ) from error
    files = distribution.files
    if not files:
        raise RuntimeUnavailableError(
            f"executable distribution has no installed-file manifest: {package_name}"
        )
    observed: dict[str, JsonValue] = {}
    runtime_root = Path(sys.prefix).resolve(strict=True)
    for entry in sorted(files, key=str):
        candidate = Path(str(distribution.locate_file(entry)))
        try:
            candidate.resolve(strict=True).relative_to(runtime_root)
        except (OSError, ValueError) as error:
            raise SecurityError(
                f"distribution file escapes the isolated runtime: {package_name}/{entry}"
            ) from error
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SecurityError(
                f"distribution manifest names a non-regular file: {package_name}/{entry}"
            )
        digest, size = sha256_file(candidate)
        observed[str(entry)] = {"sha256": digest, "size_bytes": size}
    return {
        "version": distribution.version,
        "files": observed,
    }


def _worker_package_tree_evidence() -> dict[str, JsonValue]:
    root = Path(__file__).resolve(strict=True).parents[1]
    observed: dict[str, JsonValue] = {}
    for candidate in sorted(root.rglob("*")):
        relative = candidate.relative_to(root).as_posix()
        if "__pycache__" in candidate.parts or candidate.suffix in {".pyc", ".pyo"}:
            continue
        if candidate.is_dir():
            continue
        if candidate.is_symlink() or not candidate.is_file():
            raise SecurityError(f"worker package contains an unsafe path: {candidate}")
        digest, size = sha256_file(candidate)
        observed[relative] = {"sha256": digest, "size_bytes": size}
    if not observed:
        raise RuntimeUnavailableError("worker package byte evidence is empty")
    return {"root_name": root.name, "files": observed}


def _installed_distribution_names() -> tuple[str, ...]:
    """Return every installed distribution under a canonical stable name.

    Model runtimes execute substantial transitive code (for example MLX,
    Transformers, Torch, NumPy, and audio codecs), not only the top-level model
    package.  A version-only ``pip freeze`` is insufficient byte identity, so
    the per-trial executed-code manifest covers the complete installed graph.
    """

    names: set[str] = set()
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise RuntimeUnavailableError(
                "installed distribution is missing canonical name metadata"
            )
        names.add(re.sub(r"[-_.]+", "-", raw_name).lower())
    if not names:
        raise RuntimeUnavailableError("isolated runtime contains no attestable distributions")
    return tuple(sorted(names))


def _executed_code_evidence(definition: WorkerDefinition) -> dict[str, JsonValue]:
    package_names = _installed_distribution_names()
    required = {"cole-voice-lab"}
    if definition.package_name is not None:
        required.add(re.sub(r"[-_.]+", "-", definition.package_name).lower())
    missing = required.difference(package_names)
    if missing:
        raise RuntimeUnavailableError(
            f"isolated runtime omits required executable distributions: {sorted(missing)}"
        )
    evidence: dict[str, JsonValue] = {
        "schema_version": "cole-voice-lab.executed-code-evidence/v1",
        "worker_package": _worker_package_tree_evidence(),
        "distributions": {name: _distribution_file_evidence(name) for name in package_names},
    }
    evidence["integrity_sha256"] = canonical_sha256(evidence)
    return evidence


def _accelerator_synchronize() -> None:  # pragma: no cover - optional hardware runtimes
    mlx = sys.modules.get("mlx.core")
    if mlx is not None and hasattr(mlx, "synchronize"):
        mlx.synchronize()
    torch = sys.modules.get("torch")
    if torch is not None and hasattr(torch, "mps") and torch.backends.mps.is_available():
        torch.mps.synchronize()


def execute_request(
    definition: WorkerDefinition,
    request: ExperimentRequest,
    *,
    load: LoadFunction,
    synthesize: SynthesizeFunction,
    context: WorkerContext | None = None,
    synchronize: SynchronizeFunction = _accelerator_synchronize,
) -> ExperimentResult:
    """Execute one request; model state is intentionally not returned or cached."""

    _validate_request(definition, request)
    for key, value in {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
    }.items():
        os.environ[key] = value
    active_context = context or WorkerContext.from_environment(definition, request)
    package_version = _installed_package_version(definition)
    executed_code_before = _executed_code_evidence(definition)
    reference_before, source_before, source_size_before = _verify_human_bindings(request)
    output_path = Path(request.output_path).expanduser().resolve(strict=False)
    if output_path.exists():
        raise ContractError(f"refusing to overwrite worker output: {output_path}")
    output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    load_started = time.monotonic()
    model = load(request, active_context)
    synchronize()
    loaded_at = time.monotonic()
    load_seconds = loaded_at - load_started
    timing_mode = request.settings.get("timing_mode", "cold")
    warmup_seconds = 0.0
    if timing_mode == "warm":
        warm_started = time.monotonic()
        synthesize(model, request, active_context, None)
        synchronize()
        warmup_seconds = time.monotonic() - warm_started
        measured_started = time.monotonic()
    else:
        measured_started = load_started
    synthesis_started = time.monotonic()
    realized_seed = synthesize(model, request, active_context, output_path)
    synchronize()
    finished = time.monotonic()
    synthesis_seconds = finished - synthesis_started
    generation_seconds = finished - measured_started

    try:
        reference_after, source_after, source_size_after = _verify_human_bindings(request)
        if (
            reference_after != reference_before
            or source_after != source_before
            or source_size_after != source_size_before
        ):
            raise SecurityError("reference or source audio changed during inference")
        inspection = inspect_pcm_wav(output_path)
        output_digest, output_bytes = sha256_file(output_path)
        executed_code_after = _executed_code_evidence(definition)
        if executed_code_after != executed_code_before:
            raise SecurityError("executed package bytes changed during inference")
        environment = _environment_evidence(output_path, request.trial_id)
        executed_code_path = (
            output_path.parent / "evidence" / f"{request.trial_id}.executed-code.json"
        )
        executed_code_digest = write_canonical_json(executed_code_path, executed_code_before)
        model_evidence_path = (
            output_path.parent / "evidence" / f"{request.trial_id}.model-evidence.json"
        )
        model_evidence: dict[str, JsonValue] = {
            "schema_version": "cole-voice-lab.worker-model-evidence/v1",
            "candidate_id": request.candidate_id,
            "model_id": request.model_id,
            "model_revision": request.model_revision,
            "code_revision": request.code_revision,
            "model_manifest_sha256": active_context.model_manifest_sha256,
            "auxiliary_manifest_sha256": dict(active_context.auxiliary_manifest_sha256),
            "code_checkout_integrity_sha256": (active_context.code_checkout_integrity_sha256),
            "executed_code_evidence_path": f"evidence/{request.trial_id}.executed-code.json",
            "executed_code_evidence_sha256": executed_code_digest,
            "reference_sha256": request.reference.sha256,
            **request.reference.consent_binding(),
            "source_audio_sha256": source_before,
            "source_audio_size_bytes": source_size_before,
            "settings": request.settings,
            "runtime_package": definition.package_name,
            "runtime_package_version": package_version,
        }
        if definition.watermarked is not None:
            model_evidence["watermarked"] = definition.watermarked
        if model_evidence_path.exists():
            raise ContractError(f"refusing to overwrite model evidence: {model_evidence_path}")
        model_evidence_digest = write_canonical_json(model_evidence_path, model_evidence)
    except Exception:
        output_path.unlink(missing_ok=True)
        for evidence_name in (
            f"{request.trial_id}.pip-freeze.txt",
            f"{request.trial_id}.executed-code.json",
            f"{request.trial_id}.model-evidence.json",
        ):
            (output_path.parent / "evidence" / evidence_name).unlink(missing_ok=True)
        raise

    metadata: dict[str, JsonValue] = {
        "profile": request.settings.get("profile", "quality"),
        "timing_mode": timing_mode,
        "timing_semantics": request.settings.get("timing_semantics", "unspecified"),
        "model_load_seconds": load_seconds,
        "warmup_seconds": warmup_seconds,
        "timed_synthesis_seconds": synthesis_seconds,
        "model_evidence_path": f"evidence/{request.trial_id}.model-evidence.json",
        "model_evidence_sha256": model_evidence_digest,
        "model_manifest_sha256": active_context.model_manifest_sha256,
        "auxiliary_manifest_sha256": dict(active_context.auxiliary_manifest_sha256),
        "code_checkout_integrity_sha256": active_context.code_checkout_integrity_sha256,
        "executed_code_evidence_path": f"evidence/{request.trial_id}.executed-code.json",
        "executed_code_evidence_sha256": executed_code_digest,
        "reference_sha256": request.reference.sha256,
        **request.reference.consent_binding(),
        "source_audio_sha256": source_before,
        "source_audio_size_bytes": source_size_before,
        "runtime_package": definition.package_name,
        "runtime_package_version": package_version,
        "worker_lifecycle": "one-request-then-exit",
    }
    if definition.watermarked is not None:
        metadata["watermarked"] = definition.watermarked
    return ExperimentResult(
        trial_id=request.trial_id,
        candidate_id=request.candidate_id,
        ok=True,
        output_path=str(output_path),
        output_sha256=output_digest,
        output_bytes=output_bytes,
        audio=inspection.audio,
        generation_seconds=generation_seconds,
        realized_seed=realized_seed,
        environment=environment,
        metadata=metadata,
    )


def _failure_result(request: ExperimentRequest, error: Exception) -> ExperimentResult:
    if isinstance(error, SecurityError):
        code = "security_error"
    elif isinstance(error, ContractError):
        code = "contract_error"
    elif isinstance(error, RuntimeUnavailableError):
        code = "runtime_unavailable"
    else:
        code = "inference_failed"
    return ExperimentResult(
        trial_id=request.trial_id,
        candidate_id=request.candidate_id,
        ok=False,
        error_code=code,
        error_message=str(error)[:4_000] or error.__class__.__name__,
    )


def read_request(stream: TextIO = sys.stdin) -> ExperimentRequest:
    payload = stream.read(_MAX_REQUEST_BYTES + 1)
    if not isinstance(payload, str):
        raise ContractError("worker request must be UTF-8 JSON text")
    if len(payload.encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise ContractError("worker request exceeds protocol size limit")
    return ExperimentRequest.model_validate_json(payload)


def worker_main(
    definition: WorkerDefinition,
    *,
    load: LoadFunction,
    synthesize: SynthesizeFunction,
) -> int:
    """Read once, emit once, and return so the process releases model memory."""

    try:
        request = read_request()
    except Exception as error:
        print(f"invalid worker request: {error}", file=sys.stderr)
        return 2
    try:
        with redirect_stdout(sys.stderr):
            result = execute_request(
                definition,
                request,
                load=load,
                synthesize=synthesize,
            )
    except Exception as error:
        if not isinstance(error, VoiceLabError):
            traceback.print_exc(file=sys.stderr)
        result = _failure_result(request, error)
    sys.stdout.buffer.write(canonical_json_bytes(result) + b"\n")
    sys.stdout.flush()
    return 0


@contextmanager
def local_huggingface_overrides(paths: Mapping[str, Path]) -> Iterator[None]:
    """Route model-internal Hub calls to already verified local snapshots."""

    huggingface_hub = cast(
        Any,
        importlib.import_module("huggingface_hub"),
    )  # pragma: no cover - optional runtime dependency

    original_snapshot = huggingface_hub.snapshot_download
    original_file = huggingface_hub.hf_hub_download

    def snapshot_download(repo_id: str, *args: object, **kwargs: object) -> str:
        del args, kwargs
        try:
            return str(paths[repo_id])
        except KeyError as error:
            raise RuntimeUnavailableError(
                f"unprovisioned Hub snapshot requested: {repo_id}"
            ) from error

    def hf_hub_download(repo_id: str, filename: str, *args: object, **kwargs: object) -> str:
        del args, kwargs
        try:
            candidate = paths[repo_id] / filename
        except KeyError as error:
            raise RuntimeUnavailableError(f"unprovisioned Hub file requested: {repo_id}") from error
        if not candidate.is_file():
            raise RuntimeUnavailableError(f"curated Hub file is missing: {repo_id}/{filename}")
        return str(candidate)

    huggingface_hub.snapshot_download = snapshot_download
    huggingface_hub.hf_hub_download = hf_hub_download
    try:
        yield
    finally:
        huggingface_hub.snapshot_download = original_snapshot
        huggingface_hub.hf_hub_download = original_file
