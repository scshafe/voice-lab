"""Operator CLI. Mutating and network actions are explicit subcommands/flags."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast, get_args

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from . import cli_home_host
from .audition import (
    AuditionPlan,
    audition_paths,
    build_audition_plan,
    load_validated_audition_run,
    persist_audition_plan,
    run_offline_audition,
)
from .blind import (
    ScoreSheet,
    aggregate_scores,
    blind_inputs_from_audition,
    build_review_package,
    verify_review_package,
)
from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    open_regular_no_follow,
    sha256_file,
    write_canonical_json,
)
from .capture import (
    TakeEngine,
    capture_prompt_suite,
    list_avfoundation_audio_devices,
    require_legacy_capture_root,
    require_legacy_capture_suite,
    select_umik1_device,
)
from .consent import (
    approve_self_consent,
    consent_record_path,
    install_pending_consent,
    revoke_consent,
)
from .datasets import (
    DEFAULT_MINIMUM_HOLDOUT_SECONDS,
    DatasetSelectionDocument,
    PilotGateApproval,
    build_dataset_manifest,
    build_transfer_plan,
    load_pilot_gate,
    publish_dataset_manifest,
    publish_transfer_plan,
    validate_published_dataset,
)
from .diagnostic_roots import (
    DIAGNOSTIC_ROOT_MARKER,
    initialize_diagnostic_root,
    require_catalog_root,
    require_corpus_eligible_package,
    require_root_purpose,
)
from .doctor import run_doctor
from .errors import VoiceLabError
from .intake import accept_capture_session, validate_capture_package
from .models import (
    PHYSICAL_TEST_SUITE_PREFIX,
    PROMPT_SUITE_V1,
    PROMPT_SUITE_V2,
    CaptureDevice,
    ConsentRecord,
    Prompt,
    PromptSuite,
    PromptSuitePurpose,
    ReferenceEvidence,
    StableId,
    utc_now,
)
from .paths import ASSET_DIRECTORIES, AssetLayout
from .promotion import PromotionManifest, build_chloespeech_handoff, validate_promotion
from .prompt_catalogs import BUILTIN_PROMPT_SUITE_SHA256, load_builtin_prompt_suite
from .references import (
    HeadphoneReviewEvidence,
    load_accepted_utterance,
    publish_reference,
    validate_reference_publication,
)
from .registry import CandidateRegistry, load_registry
from .resources import load_prompt_suite
from .runtime import (
    build_runtime_eviction_plan,
    build_runtime_install_plan,
    build_source_eviction_plan,
    build_source_provision_plan,
    exclusive_worker_slot,
    execute_provision_plan,
    execute_runtime_eviction,
    execute_runtime_install_plan,
    execute_source_eviction,
    plan_commands,
    verify_local_source,
    verify_runtime_install_receipt,
)
from .storage_contracts import DEFAULT_VAULT_DESTINATION
from .storage_ports import StorageError, storage_error
from .sync import (
    PRIMARY_MINI_ALIAS,
    build_sync_plan,
    execute_sync_plan,
)

if TYPE_CHECKING:
    from .catalog_ports import (
        CatalogEditor,
        CatalogReader,
        CatalogRecord,
        CorpusStoreCode,
        PromptRecord,
    )
    from .recording_contracts import CatalogReference, CatalogSnapshot
    from .storage_binding import LibraryKeyCreation, LibraryVault
    from .storage_contracts import StorageBinding, VaultDestinationApproval

Command = Callable[[argparse.Namespace], int]
MAX_CLI_DATASET_JSON_BYTES = 16 * 1024 * 1024
# A recovery sheet or one vault token, as it arrives on standard input.
MAX_CLI_SECRET_INPUT_BYTES = 64 * 1024
# The file classes a vault binding or a destination approval may name.
VAULT_FILE_CLASSES = ("derived-edits", "prompt-database", "recordings", "records", "training")
VAULT_BACKENDS = ("local-directory", "tailnet-vault")
# `prompts show` names built-ins only; unversioned names stay v1 (docs/PROMPT_CATALOG_V2.md §4).
PROMPT_SHOW_RESOURCES = {
    "pilot": "pilot-v1.json",
    "evaluation": "evaluation-v1.json",
    "pilot-v1": "pilot-v1.json",
    "evaluation-v1": "evaluation-v1.json",
    "pilot-v2": "pilot-v2.json",
    "physical-test-v1": "physical-test-v1.json",
}
# `--catalog` / `--baseline` name a released versioned built-in, or a published catalog.
BUILTIN_CATALOG_RESOURCES: Mapping[str, str] = {
    name.removesuffix(".json"): name for name in BUILTIN_PROMPT_SUITE_SHA256
}
UNKNOWN_CATALOG_MESSAGE = "not a released built-in prompt suite or a published catalog"
DEFAULT_CATALOG_ID = "cole-corpus"
BUILTIN_PROMPT_SOURCES = {"pilot-v1": "pilot-v1", "pilot-v2": "pilot-v2"}
# Catalog versions and prompt revisions are SQLite integers (corpus_store.MAX_CATALOG_VERSION).
MAX_CLI_VERSION = 2**63 - 1
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]*\.[A-Z][A-Z0-9_]*$")

type CatalogCommandCode = Literal[
    "CATALOG_CLI.USAGE_INVALID",
    "CATALOG_CLI.CATALOG_ID_RESERVED",
]
type VaultCommandCode = Literal[
    "VAULT_CLI.USAGE_INVALID",
    "VAULT_CLI.NOT_A_TERMINAL",
]


class CatalogCommandError(VoiceLabError):
    """A refusal the catalog commands decide themselves; ``code`` names it.

    ``CATALOG_CLI.USAGE_INVALID``: a flag combination the command refuses.
    ``CATALOG_CLI.CATALOG_ID_RESERVED``: a new catalog named like a released built-in (the
    CLI resolves built-in names first, so only the CLI can reserve them).
    Every rule about what the store may hold is the store's own and stays a
    ``CorpusStoreError`` (``CORPUS.*``): the typo-class bound of a keep-recordings edit,
    every live prompt fitting every catalog, and no two live prompts reading the same.
    """

    def __init__(self, code: CatalogCommandCode, message: str) -> None:
        self.code: CatalogCommandCode = code
        super().__init__(message)


class VaultCommandError(VoiceLabError):
    """A refusal the vault commands decide themselves; ``code`` names it.

    ``VAULT_CLI.USAGE_INVALID``: a flag combination the command refuses (the two ways of
    naming a generation, a preview that also approves, a key with no output, a namespace
    that is neither bound nor named).
    ``VAULT_CLI.NOT_A_TERMINAL``: the recovery sheet was to be printed, but standard output
    is not a terminal, so a tool or a log would receive the library key.
    Every rule about the vault itself stays a ``StorageError`` (``STORAGE.*``): what is
    bound, approved, reachable or intact is the storage layer's business.
    """

    def __init__(self, code: VaultCommandCode, message: str) -> None:
        self.code: VaultCommandCode = code
        super().__init__(message)


def _error_text(error: BaseException) -> str:
    """``<CODE>: <message>`` for an error that names itself, else the message alone."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and _ERROR_CODE.match(code):
        return f"{code}: {error}"
    return str(error)


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _registry(options: argparse.Namespace) -> CandidateRegistry:
    root = _repository_root()
    candidates = (
        Path(options.candidates).expanduser()
        if options.candidates
        else root / "config/candidates.toml"
    )
    lock = (
        Path(options.model_lock).expanduser()
        if options.model_lock
        else root / "config/model-sources.lock.json"
    )
    return load_registry(candidates, lock)


def _json_print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid ISO-8601 timestamp: {value}") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("timestamp must include an explicit UTC offset")
    return parsed


def _version_number(text: str) -> int | None:
    """``text`` as a catalog version or prompt revision, or ``None`` when it is not one.

    The one spelling every version flag and ``<catalog_id>@<version>`` accept: ASCII digits
    without a leading zero, 1 to ``MAX_CLI_VERSION`` (so ``7`` names version 7 and ``07``
    names nothing).
    """
    if (
        not text.isascii()
        or not text.isdecimal()
        or text[0] == "0"
        or len(text) > len(str(MAX_CLI_VERSION))
        or int(text) > MAX_CLI_VERSION
    ):
        return None
    return int(text)


def _sha256_argument(value: str) -> str:
    """An argparse type for a lowercase SHA-256 hex digest."""
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError(f"expected 64 lowercase hexadecimal characters: {value}")
    return value


def _positive_number(value: str) -> int:
    """An argparse type for a catalog version or prompt revision (``_version_number``)."""
    number = _version_number(value)
    if number is None:
        raise argparse.ArgumentTypeError(
            f"expected a positive whole number of at most {MAX_CLI_VERSION}, without a "
            f"leading zero: {value}"
        )
    return number


def _asset_root(value: str | None) -> Path:
    configured = value or os.environ.get("COLE_VOICE_LAB_ASSET_ROOT")
    if not configured:
        raise VoiceLabError("an explicit --asset-root or COLE_VOICE_LAB_ASSET_ROOT is required")
    return Path(configured).expanduser()


def _read_bounded_contract[ContractT: BaseModel](
    path: Path, model_type: type[ContractT]
) -> ContractT:
    """Read one strict operator-supplied JSON contract through a bounded descriptor.

    A FIFO or device is refused (``SecurityError``) without blocking on its open.
    """

    source = path.expanduser()
    with open_regular_no_follow(source, nonblocking=True) as stream:
        if os.fstat(stream.fileno()).st_size > MAX_CLI_DATASET_JSON_BYTES:
            raise VoiceLabError(f"JSON input exceeds {MAX_CLI_DATASET_JSON_BYTES} bytes: {source}")
        payload = stream.read(MAX_CLI_DATASET_JSON_BYTES + 1)
    if len(payload) > MAX_CLI_DATASET_JSON_BYTES:
        raise VoiceLabError(f"JSON input exceeds {MAX_CLI_DATASET_JSON_BYTES} bytes: {source}")
    return model_type.model_validate_json(payload)


def _new_output_path(path: Path) -> Path:
    """Resolve one new output path while rejecting symlinked parent components."""

    output = Path(os.path.abspath(path.expanduser()))
    if output == Path(output.anchor):
        raise VoiceLabError("output must name a file below an existing private directory")
    cursor = Path(output.anchor)
    try:
        for component in output.parent.parts[1:]:
            cursor /= component
            metadata = os.lstat(cursor)
            if stat.S_ISLNK(metadata.st_mode):
                raise VoiceLabError(f"output parent contains a symlink: {cursor}")
            if not stat.S_ISDIR(metadata.st_mode):
                raise VoiceLabError(f"output parent component is not a directory: {cursor}")
    except FileNotFoundError as error:
        raise VoiceLabError(f"output parent must already exist: {output.parent}") from error
    parent_metadata = os.lstat(output.parent)
    if parent_metadata.st_uid != os.getuid() or stat.S_IMODE(parent_metadata.st_mode) & 0o077:
        raise VoiceLabError(
            "handoff output parent must be user-owned and inaccessible to group and other"
        )
    try:
        os.lstat(output)
    except FileNotFoundError:
        pass
    else:
        raise VoiceLabError(f"refusing to overwrite existing handoff output: {output}")
    return output


def _write_new_canonical_json(path: Path, value: object) -> tuple[Path, str]:
    """Write canonical private JSON to one new path without an overwrite window."""

    output = _new_output_path(path)
    payload = canonical_json_bytes(value) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(output, flags, 0o600)
    except FileExistsError as error:
        raise VoiceLabError(f"refusing to overwrite existing handoff output: {output}") from error
    except OSError as error:
        raise VoiceLabError(f"cannot create handoff output {output}: {error}") from error

    opened = os.fstat(descriptor)
    descriptor_owned = True
    try:
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise VoiceLabError("handoff output must be one regular non-hardlinked file")
        os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "wb", closefd=True)
        descriptor_owned = False
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        if descriptor_owned:
            os.close(descriptor)
        try:
            current = os.lstat(output)
            if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                output.unlink()
        except FileNotFoundError:
            pass
        raise

    try:
        published = os.lstat(output)
        if (
            not stat.S_ISREG(published.st_mode)
            or published.st_nlink != 1
            or stat.S_IMODE(published.st_mode) != 0o600
            or (published.st_dev, published.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise VoiceLabError("handoff output changed during publication")
        parent_descriptor = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as error:
        raise VoiceLabError(f"cannot finalize handoff output {output}: {error}") from error
    return output, hashlib.sha256(payload).hexdigest()


def _prompt_suite_mappings(values: list[str]) -> dict[str, PromptSuite]:
    suites: dict[str, PromptSuite] = {}
    for value in values:
        suite_id, separator, path_value = value.partition("=")
        if not separator or not suite_id or not path_value:
            raise VoiceLabError("--prompt-suite must use the exact SUITE_ID=PATH form")
        if suite_id in suites:
            raise VoiceLabError(f"duplicate --prompt-suite mapping for {suite_id!r}")
        suite = _read_bounded_contract(Path(path_value), PromptSuite)
        if suite.suite_id != suite_id:
            raise VoiceLabError(
                f"prompt-suite mapping key {suite_id!r} does not match {suite.suite_id!r}"
            )
        suites[suite_id] = suite
    return suites


def _selected_prompts(suite: PromptSuite, prompt_ids: list[str]) -> tuple[Prompt, ...]:
    if len(prompt_ids) != len(set(prompt_ids)):
        raise VoiceLabError("audition prompt selection must be unique")
    if not prompt_ids:
        return suite.prompts
    selected = set(prompt_ids)
    prompts = tuple(prompt for prompt in suite.prompts if prompt.prompt_id in selected)
    missing = sorted(selected - {prompt.prompt_id for prompt in prompts})
    if missing:
        raise VoiceLabError(f"unknown audition prompt IDs: {missing}")
    return prompts


def _audition_references(
    layout: AssetLayout, reference_ids: list[str]
) -> tuple[ReferenceEvidence, ...]:
    if len(reference_ids) != len(set(reference_ids)):
        raise VoiceLabError("audition reference selection must be unique")
    references: list[ReferenceEvidence] = []
    for reference_id in reference_ids:
        published = validate_reference_publication(
            layout,
            layout.directory("references") / reference_id,
        )
        manifest = published.manifest
        references.append(
            ReferenceEvidence(
                path=str((layout.root / manifest.output.path).resolve(strict=True)),
                sha256=manifest.output.sha256,
                transcript=manifest.source.transcript,
                subject_id=manifest.source.subject_id,
                consent_path=str((layout.root / manifest.consent.file.path).resolve(strict=True)),
                consent_sha256=manifest.consent.file.sha256,
            )
        )
    return tuple(references)


def _audition_sources(
    layout: AssetLayout,
    values: list[str],
    prompt_ids: set[str],
) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for value in values:
        prompt_id, separator, path_value = value.partition("=")
        if not separator or not prompt_id or not path_value:
            raise VoiceLabError("--source-audio must use the exact PROMPT_ID=PATH form")
        if prompt_id in sources:
            raise VoiceLabError(f"duplicate performed source for {prompt_id!r}")
        try:
            path = Path(path_value).expanduser().resolve(strict=True)
            path.relative_to(layout.root)
        except (OSError, ValueError) as error:
            raise VoiceLabError(
                f"performed source must be an existing file beneath the asset root: {path_value}"
            ) from error
        sources[prompt_id] = path
    extra = sorted(set(sources) - prompt_ids)
    if extra:
        raise VoiceLabError(f"performed sources name unselected prompt IDs: {extra}")
    return sources


def _load_audition_plan(layout: AssetLayout, experiment_id: str) -> AuditionPlan:
    paths = audition_paths(layout, experiment_id)
    plan = _read_bounded_contract(paths.plan_path, AuditionPlan)
    if plan.experiment_id != experiment_id:
        raise VoiceLabError("audition plan experiment ID does not match its directory")
    return plan


def _doctor(options: argparse.Namespace) -> int:
    asset_root = Path(options.asset_root).expanduser() if options.asset_root else None
    checks = run_doctor(asset_root)
    if options.json:
        _json_print({"checks": [check.to_json() for check in checks]})
    else:
        for check in checks:
            print(f"{check.status.upper():6} {check.check_id:12} {check.message}")
    return 1 if any(check.status == "fail" for check in checks) else 0


def _assets_init(options: argparse.Namespace) -> int:
    if options.diagnostic is not None:
        if options.subject_id is None:
            raise VoiceLabError("--diagnostic physical-test requires --subject-id")
        diagnostic, marker = initialize_diagnostic_root(
            _asset_root(options.asset_root), subject_id=options.subject_id
        )
        # A diagnostic subject never approves consent, so no pending record is installed.
        _json_print(
            {
                "asset_root": str(diagnostic.root),
                "directories": [str(diagnostic.directory(name)) for name in ASSET_DIRECTORIES],
                "diagnostic_root": marker.model_dump(mode="json"),
                "diagnostic_root_path": DIAGNOSTIC_ROOT_MARKER,
            }
        )
        return 0
    if options.subject_id is not None:
        raise VoiceLabError("--subject-id is recorded only for --diagnostic asset roots")
    layout = AssetLayout.initialize(_asset_root(options.asset_root))
    consent_path, consent_sha256 = install_pending_consent(layout)
    _json_print(
        {
            "asset_root": str(layout.root),
            "directories": [str(layout.directory(name)) for name in ASSET_DIRECTORIES],
            "pending_consent": str(consent_path),
            "pending_consent_sha256": consent_sha256,
        }
    )
    return 0


def _assets_status(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    _json_print(
        {
            "asset_root": str(layout.root),
            "directories": {
                name: {
                    "path": str(layout.directory(name)),
                    "entries": len(tuple(layout.directory(name).iterdir())),
                }
                for name in ASSET_DIRECTORIES
            },
        }
    )
    return 0


def _effective_labels(suite: PromptSuite, prompt: Prompt) -> list[str]:
    """Presentation labels in docs/PROMPT_CATALOG_V2.md §6.1 order; behavior never reads them."""
    return [
        prompt.category,
        *(["test"] if suite.purpose == "physical-test" else []),
        *(label for label in prompt.labels if label != "test"),
        *(["reference-candidate"] if prompt.reference_candidate else []),
        *(["holdout"] if prompt.holdout else []),
    ]


@dataclass(frozen=True, slots=True)
class _ResolvedCatalog:
    """A catalog named on the command line: a built-in suite, or a published version."""

    suite: PromptSuite
    snapshot: CatalogSnapshot | None
    reference: CatalogReference | None

    @property
    def catalog(self) -> PromptSuite | CatalogSnapshot:
        return self.suite if self.snapshot is None else self.snapshot


def _is_stable_id(value: str) -> bool:
    try:
        TypeAdapter(StableId).validate_python(value)
    except ValidationError:
        return False
    return True


def _unknown_catalog(
    name: str, builtins: Mapping[str, str], code: CorpusStoreCode, detail: str | None = None
) -> VoiceLabError:
    """The refusal of a ``--catalog``-style name, keeping the store's code for the cause."""
    from .catalog_ports import CorpusStoreError

    explained = "" if detail is None else f" ({detail})"
    return CorpusStoreError(
        code,
        f"{UNKNOWN_CATALOG_MESSAGE}: {name}{explained}; built-in names: {', '.join(builtins)}",
    )


def _parse_catalog_name(name: str, builtins: Mapping[str, str]) -> tuple[str, int | None]:
    """Split ``<catalog_id>[@<version>]``; anything else is an unknown catalog."""
    catalog_id, separator, version_text = name.partition("@")
    if not _is_stable_id(catalog_id):
        raise _unknown_catalog(name, builtins, "CORPUS.CATALOG_UNKNOWN")
    if not separator:
        return catalog_id, None
    version = _version_number(version_text)
    if version is None:
        raise _unknown_catalog(
            name,
            builtins,
            "CORPUS.VERSION_UNKNOWN",
            f"a version is a positive whole number of at most {MAX_CLI_VERSION}, without a "
            "leading zero",
        )
    return catalog_id, version


def _resolve_catalog(
    name: str, asset_root: str | None, builtins: Mapping[str, str]
) -> _ResolvedCatalog:
    """Resolve a ``--catalog``-style name: an exact built-in name wins, then the store.

    A built-in never touches the asset root or the corpus store. A published catalog is
    ``<catalog_id>`` (its latest version) or ``<catalog_id>@<version>``; it is resolved
    through the library's catalog (``storage_binding.open_catalog_reader``: the store's
    lease and lock are taken and its database opened, nothing is created where no store
    exists), which is closed again before the caller reads any evidence.
    Raises: ``CorpusStoreError`` naming ``UNKNOWN_CATALOG_MESSAGE`` and the built-in names
    for an unknown name (``CORPUS.CATALOG_UNKNOWN`` / ``CORPUS.VERSION_UNKNOWN``, or
    ``CORPUS.STORE_MISSING`` when there is no library or store); other store and snapshot
    failures unchanged.
    """
    resource = builtins.get(name)
    if resource is not None:
        return _ResolvedCatalog(load_builtin_prompt_suite(resource), None, None)
    catalog_id, version = _parse_catalog_name(name, builtins)
    if not (asset_root or os.environ.get("COLE_VOICE_LAB_ASSET_ROOT")):
        raise _unknown_catalog(
            name,
            builtins,
            "CORPUS.STORE_MISSING",
            "published catalogs are read from the --asset-root or COLE_VOICE_LAB_ASSET_ROOT "
            "library",
        )
    from .catalog_ports import CorpusStoreError
    from .storage_binding import open_catalog_reader

    layout = AssetLayout.validate(_asset_root(asset_root))
    try:
        with open_catalog_reader(layout) as store:
            reference = store.resolve_reference(catalog_id, version)
            snapshot = store.materialize(reference)
    except CorpusStoreError as error:
        if error.code in (
            "CORPUS.STORE_MISSING",
            "CORPUS.CATALOG_UNKNOWN",
            "CORPUS.VERSION_UNKNOWN",
        ):
            raise _unknown_catalog(name, builtins, error.code, str(error)) from error
        raise
    return _ResolvedCatalog(snapshot.suite, snapshot, reference)


def _print_suite(suite: PromptSuite) -> None:
    total = sum(prompt.expected_seconds for prompt in suite.prompts)
    print(f"{suite.suite_id} · {len(suite.prompts)} prompts · {total / 60:.1f} expected minutes")
    for prompt in suite.prompts:
        if suite.schema_version == PROMPT_SUITE_V1:
            # v1 output stays byte-identical, room tone text included.
            marker = " [reference]" if prompt.reference_candidate else ""
            print(
                f"\n{prompt.prompt_id} · {prompt.category}{marker}\n{prompt.text}\n  {prompt.style}"
            )
            continue
        print(f"\n{prompt.prompt_id} · {', '.join(_effective_labels(suite, prompt))}")
        print(prompt.text if prompt.spoken else "(no spoken prompt)")
        if prompt.instructions is not None:
            print(f"  Instructions: {prompt.instructions}")
        print(f"  Guidance: {prompt.style}")


def _prompts_show(options: argparse.Namespace) -> int:
    suite = _resolve_catalog(options.suite, options.asset_root, PROMPT_SHOW_RESOURCES).suite
    if options.json:
        _json_print(suite.model_dump(mode="json"))
        return 0
    _print_suite(suite)
    return 0


def _finetune_hyperparameters(options: argparse.Namespace) -> Any:
    from .finetune import FineTuneHyperparameters

    overrides = {
        "epochs": options.epochs,
        "learning_rate": options.learning_rate,
        "batch_frames": options.batch_frames,
        "max_samples_per_batch": options.max_samples_per_batch,
        "warmup_updates": options.warmup_updates,
        "save_per_updates": options.save_per_updates,
        "keep_last_n_checkpoints": options.keep_last_n_checkpoints,
        "last_per_updates": options.last_per_updates,
        "dataloader_workers": options.dataloader_workers,
        "light_checkpoints": None if options.no_light_checkpoints is False else False,
    }
    return FineTuneHyperparameters(**{k: v for k, v in overrides.items() if v is not None})


def _finetune_run_id(options: argparse.Namespace) -> str:
    return options.run_id or utc_now().strftime("finetune-%Y%m%dt%H%M%Sz")


def _finetune_corpus_summary(corpus: Any) -> dict[str, JsonValue]:
    reference = corpus.catalog_reference
    return {
        "subject_id": corpus.subject_id,
        "catalog_suite_id": corpus.catalog_suite_id,
        "catalog_sha256": corpus.catalog_sha256,
        "catalog_reference": None if reference is None else reference.model_dump(mode="json"),
        "corpus_sha256": canonical_sha256(corpus),
        "selected_prompt_count": len(corpus.entries),
        "spoken_prompt_count": corpus.spoken_prompt_count,
        "retained_seconds": round(corpus.retained_seconds, 1),
        "native_count": sum(entry.source_kind == "native" for entry in corpus.entries),
        "legacy_count": sum(entry.source_kind == "legacy" for entry in corpus.entries),
        "missing_prompt_ids": list(corpus.missing_prompt_ids),
        "unknown_record_ids": list(corpus.unknown_record_ids),
        "complete": corpus.complete,
    }


def _finetune_select(options: argparse.Namespace) -> Any:
    from .finetune import require_complete_corpus, select_finetune_corpus

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    resolved = _resolve_catalog(options.catalog, options.asset_root, BUILTIN_CATALOG_RESOURCES)
    corpus = select_finetune_corpus(
        layout, resolved.catalog, options.subject_id, reference=resolved.reference
    )
    require_complete_corpus(corpus, allow_missing=options.allow_missing)
    return layout, corpus


def _finetune_corpus(options: argparse.Namespace) -> int:
    _layout, corpus = _finetune_select(options)
    summary = _finetune_corpus_summary(corpus)
    if options.json:
        _json_print({**summary, "corpus": corpus.model_dump(mode="json")})
    else:
        print(
            f"{summary['selected_prompt_count']}/{summary['spoken_prompt_count']} spoken prompts "
            f"have a latest kept take · {summary['retained_seconds']} s · "
            f"native {summary['native_count']} · old-app {summary['legacy_count']}"
        )
        for entry in corpus.entries:
            when = "unknown date" if entry.recorded_at is None else entry.recorded_at.isoformat()
            print(f"  {entry.prompt_id:<30} {entry.source_kind:<7} {when}  {entry.take_id}")
        for prompt_id in corpus.missing_prompt_ids:
            print(f"  {prompt_id:<30} MISSING")
    return 0


def _finetune_dataset(options: argparse.Namespace) -> int:
    from .finetune import materialize_finetune_dataset

    run_id = _finetune_run_id(options)
    # Rewriting dataset/ in place would leave run.json, and the archive record built from it,
    # naming clips the run no longer has. Asked before the corpus is selected, so an archived
    # run is named rather than whatever the selection would say first.
    _refuse_an_archived_run(
        AssetLayout.validate(_asset_root(options.asset_root)),
        f"{options.output_root}/{run_id}",
        run_id,
    )
    layout, corpus = _finetune_select(options)
    dataset = materialize_finetune_dataset(
        layout,
        corpus,
        f"{options.output_root}/{run_id}/dataset",
        ffmpeg=Path(options.ffmpeg),
    )
    _json_print(
        {
            "run_id": run_id,
            "dataset_root": dataset.dataset_root,
            "dataset_sha256": canonical_sha256(dataset),
            "clip_count": len(dataset.clips),
            "corpus": _finetune_corpus_summary(corpus),
            "gain_db": {clip.prompt_id: clip.gain_db for clip in dataset.clips},
        }
    )
    return 0


def _finetune_load_run_inputs(layout: AssetLayout, run_root: str) -> tuple[Any, Any]:
    """A run directory's corpus and dataset (``finetune.load_finetune_dataset``).

    ``train`` reads them before the run's ``run.json`` exists, so no run record is required.
    Raises: ``FineTuneError`` FINETUNE.SOURCE_CHANGED for a missing, invalid or unbound record.
    """
    from .finetune import load_finetune_dataset

    dataset, corpus = load_finetune_dataset(layout, run_root)
    return corpus, dataset


def _refuse_during_app_gpu_work(layout: AssetLayout) -> None:
    """Refuse GPU work while the recording app's service holds a job or runs a Speak request.

    Reads ``provenance/finetune-jobs/head.json`` and ``speech-active.json`` only (a marker
    whose service process is gone is ignored); takes no lock and writes nothing.
    Raises: ``FineTuneError`` FINETUNE.JOB_ACTIVE naming the app's job or Speak request;
    ``SecurityError``/``ContractError`` for an unsafe or invalid record.
    """
    from .finetune import FineTuneError
    from .recording_finetune_jobs import active_finetune_job, active_speech_request

    job = active_finetune_job(layout)
    if job is not None:
        raise FineTuneError(
            "FINETUNE.JOB_ACTIVE",
            f"the recording app's fine-tune {job.operation_id} is {job.state}; wait for it to "
            "finish or cancel it in the app (one training job at a time)",
        )
    speech = active_speech_request(layout)
    if speech is not None:
        raise FineTuneError(
            "FINETUNE.JOB_ACTIVE",
            f"the recording app's Speak request {speech.operation_id} is running; wait for it "
            "to finish or cancel it in the app (one GPU job at a time)",
        )


def _finetune_train(options: argparse.Namespace) -> int:
    from .finetune import describe_finetune_runtime, run_f5_finetune

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    _refuse_during_app_gpu_work(layout)
    run_root = f"{options.output_root}/{options.run_id}"
    # Training rewrites run.json and resumes from a checkpoint an archive removed, so it
    # would leave the run disagreeing with its archive record.
    _refuse_an_archived_run(layout, run_root, options.run_id)
    corpus, dataset = _finetune_load_run_inputs(layout, run_root)
    runtime = describe_finetune_runtime(Path(options.runtime_root))
    run = run_f5_finetune(
        layout,
        dataset,
        runtime,
        _finetune_hyperparameters(options),
        run_root,
        run_id=options.run_id,
        subject_id=corpus.subject_id,
        catalog_sha256=corpus.catalog_sha256,
    )
    _json_print(run.model_dump(mode="json"))
    return 0 if run.status == "finished" else 1


def _finetune_synthesize_one(
    layout: AssetLayout,
    options: argparse.Namespace,
    dataset: Any,
    runtime: Any,
    *,
    run_root: str,
    label: str,
    checkpoint: Path,
    use_ema: bool,
) -> Any:
    from .finetune import FINETUNE_DEFAULT_SAMPLE_TEXTS, synthesize_finetune_samples

    reference = options.reference_prompt_id or dataset.clips[0].prompt_id
    return synthesize_finetune_samples(
        layout,
        dataset,
        runtime,
        checkpoint,
        f"{run_root}/samples/{label}",
        label=label,
        reference_prompt_id=reference,
        texts=tuple(options.text) if options.text else FINETUNE_DEFAULT_SAMPLE_TEXTS,
        use_ema=use_ema,
        nfe_steps=options.nfe_steps,
        device=options.device,
    )


def _refuse_an_archived_run(layout: AssetLayout, run_root: str, run_id: str) -> None:
    """Refuse a command that writes into, or reads a checkpoint of, an archived run.

    Raises: ``FineTuneError`` FINETUNE.RUN_ARCHIVED when the run's checkpoints were removed
    after its vault copy was verified.
    """
    from .finetune import FineTuneError, archived_run_message, finetune_run_location

    if finetune_run_location(layout, run_root).location == "archived":
        raise FineTuneError("FINETUNE.RUN_ARCHIVED", archived_run_message(run_id))


def _finetune_synthesize(options: argparse.Namespace) -> int:
    from .finetune import describe_finetune_runtime

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    _refuse_during_app_gpu_work(layout)
    run_root = f"{options.output_root}/{options.run_id}"
    # Every --checkpoint value writes a sample set into the run and reads its dataset clip,
    # and `last` reads a checkpoint an archive removed. This command never reads run.json, so
    # the archive record is what it asks.
    _refuse_an_archived_run(layout, run_root, options.run_id)
    _corpus, dataset = _finetune_load_run_inputs(layout, run_root)
    runtime = describe_finetune_runtime(Path(options.runtime_root))
    if options.checkpoint == "last":
        checkpoint = layout.root / run_root / "checkpoints" / "model_last.pt"
    elif options.checkpoint == "base":
        checkpoint = runtime.pretrained_path
    else:
        checkpoint = Path(options.checkpoint)
    samples = _finetune_synthesize_one(
        layout,
        options,
        dataset,
        runtime,
        run_root=run_root,
        label=options.label,
        checkpoint=checkpoint,
        use_ema=not options.no_ema,
    )
    _json_print(samples.model_dump(mode="json"))
    return 0


def _finetune_run(options: argparse.Namespace) -> int:
    from .finetune import FINETUNE_DEFAULT_SAMPLE_TEXTS, run_finetune_pipeline

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    _refuse_during_app_gpu_work(layout)
    run_id = _finetune_run_id(options)
    # The whole pipeline rewrites dataset/, run.json and checkpoints/ of an existing run id,
    # so it does everything `finetune train` is refused for and more.
    _refuse_an_archived_run(layout, f"{options.output_root}/{run_id}", run_id)
    resolved = _resolve_catalog(options.catalog, options.asset_root, BUILTIN_CATALOG_RESOURCES)
    result = run_finetune_pipeline(
        layout,
        resolved.catalog,
        options.subject_id,
        Path(options.runtime_root),
        _finetune_hyperparameters(options),
        run_id=run_id,
        allow_missing=options.allow_missing,
        reference=resolved.reference,
        output_root=options.output_root,
        ffmpeg=Path(options.ffmpeg),
        reference_prompt_id=options.reference_prompt_id,
        sample_texts=tuple(options.text) if options.text else FINETUNE_DEFAULT_SAMPLE_TEXTS,
        use_ema=not options.no_ema,
        nfe_steps=options.nfe_steps,
        device=options.device,
    )
    run = result.run
    _json_print(
        {
            "run_id": run_id,
            "run_root": run.run_root,
            "status": run.status,
            "corpus": _finetune_corpus_summary(result.corpus),
            "checkpoints": [item.model_dump(mode="json") for item in run.checkpoints],
            "samples": {
                "finetuned": [item.relative_path for item in result.finetuned.samples],
                "base": [item.relative_path for item in result.base.samples],
            },
        }
    )
    return 0


def _finetune_provision_runtime(options: argparse.Namespace) -> int:
    from .finetune import (
        execute_finetune_runtime_provisioning,
        plan_finetune_runtime_provisioning,
    )

    plan = plan_finetune_runtime_provisioning(
        Path(options.runtime_root),
        python=None if options.python is None else Path(options.python),
        requirements=None if options.requirements is None else Path(options.requirements),
    )
    if not options.execute:
        _json_print({"executed": False, "plan": plan.to_json()})
        return 0
    runtime = execute_finetune_runtime_provisioning(plan, allow_downloads=options.allow_downloads)
    _json_print({"executed": True, "runtime": runtime.identity.model_dump(mode="json")})
    return 0


def _finetune_fetch_pretrained(options: argparse.Namespace) -> int:
    from .finetune import describe_finetune_runtime, fetch_finetune_pretrained

    paths = fetch_finetune_pretrained(
        Path(options.runtime_root), allow_downloads=options.allow_downloads
    )
    runtime = describe_finetune_runtime(Path(options.runtime_root))
    _json_print(
        {
            "files": [str(path) for path in paths],
            "runtime": runtime.identity.model_dump(mode="json"),
        }
    )
    return 0


def _finetune_run_root(options: argparse.Namespace) -> str:
    return f"{options.output_root}/{options.run}"


def _vault_for_training(layout: AssetLayout) -> LibraryVault:
    """The bound vault, which must copy the ``training`` class.

    Fine-tune checkpoints and datasets are a separate opt-in class: a binding (and, for a
    tailnet vault, the destination approval behind it) either covers ``training`` or no run
    ever leaves this Mac.
    Raises: ``StorageError`` (``STORAGE.NOT_BOUND``, ``STORAGE.NOT_APPROVED``).
    """
    vault = _open_bound_vault(layout)
    if "training" not in vault.classes:
        raise storage_error(
            "STORAGE.NOT_APPROVED",
            "this library's vault does not take fine-tune runs; its classes are "
            f"{', '.join(vault.classes)}. Opt in by binding it again with `voice-lab vault bind "
            "--classes … training` (a tailnet vault also needs an approval that covers training)",
        )
    return vault


class _Generation(NamedTuple):
    """The generation a push left as the vault's head."""

    number: int
    manifest_sha256: str


def _finetune_push(vault: LibraryVault, layout: AssetLayout) -> _Generation:
    """One push of the whole library, which is how a run reaches the vault.

    The fine-tune owner must have completed in this pass. A pass that meets a fine-tune job or
    a Speak request reports that owner ``changed`` and keeps the previous generation's
    entries, which is a fine outcome for a copy but not for an archive: the generation would
    then name an older run, and a removal resting on it would unlink files the vault never
    took. ``PushResult.exit_status`` does not see that (only a refused owner raises it), so it
    is checked here.
    Raises: ``VoiceLabError`` when the push did not put this Mac's view of the runs in the
    vault, or when the vault holds no generation at all.
    """
    from .library_generations import push_generation
    from .library_replication import FineTuneRunSource

    result = push_generation(
        layout,
        blob_store=vault.blob_store,
        ref_store=vault.ref_store,
        library_id=vault.library_id,
        destination=vault.destination,
        classes=vault.classes,
        chunk_bytes=vault.chunk_bytes,
    )
    if result.outcome not in ("pushed", "up-to-date") or result.exit_status != 0:
        raise VoiceLabError(
            f"the copy to the vault did not finish ({result.outcome}); run `voice-lab vault "
            "push` and read its report, then archive again"
        )
    runs = FineTuneRunSource().name
    waiting = [owner for owner in result.waiting_owners if owner.owner == runs]
    if waiting:
        raise VoiceLabError(
            f"the copy to the vault did not carry this Mac's fine-tune runs ({waiting[0].reason}"
            "), so the vault still holds the runs of an earlier generation; nothing was "
            "archived or removed"
        )
    if result.generation < 1 or result.manifest_sha256 is None:
        raise VoiceLabError(
            "the vault holds no generation of this library yet, so there is nothing to archive"
        )
    return _Generation(result.generation, result.manifest_sha256)


def _finetune_run_entries(
    records: Any, generation: int, manifest_sha256: str, run_root: str
) -> list[Any]:
    """Every manifest entry of ``run_root`` in the generation this Mac keeps a copy of.

    Raises: ``VoiceLabError`` when the copy is missing (a removal could never be undone
    without it), is not the manifest the caller's record names, or holds none of the run's
    files.
    """
    from .storage_contracts import manifest_digest

    manifest = records.read_manifest_copy(generation)
    if manifest is None:
        raise VoiceLabError(
            f"this Mac keeps no manifest copy of generation {generation} under "
            f"{records.root}/manifests, so the run's files could not be read back"
        )
    if manifest_digest(manifest) != manifest_sha256:
        raise VoiceLabError(
            f"this Mac's manifest copy of generation {generation} is not the one the run's "
            "record names"
        )
    entries = [
        entry
        for entry in manifest.entries
        if entry.path == run_root or entry.path.startswith(f"{run_root}/")
    ]
    if not entries:
        raise VoiceLabError(f"generation {generation} holds no file of {run_root}")
    return entries


def _finetune_verify_run(
    vault: LibraryVault, layout: AssetLayout, run_root: str, pushed: _Generation
) -> Any:
    """Download and decrypt every chunk of the run's entries; the vault's word counts for none.

    The generation is named rather than resolved from the head: the push and the verify take
    ``.push.lock`` separately, so the service's own vault supervisor can leave a newer
    generation as the head in between, and the entries that authorize an unlink must be
    exactly the entries the records name.
    Raises: ``VoiceLabError`` when any entry is missing or does not match, or when the
    generation's manifest is not the one the push left.
    """
    from .library_generations import verify_generation

    report = verify_generation(
        layout,
        blob_store=vault.blob_store,
        ref_store=vault.ref_store,
        library_id=vault.library_id,
        generation=pushed.number,
        paths=(run_root,),
    )
    if report.manifest_sha256 != pushed.manifest_sha256:
        raise VoiceLabError(
            f"generation {pushed.number} in the vault is not the one this push left, so "
            "nothing was archived or removed"
        )
    if report.exit_status != 0:
        problems = ", ".join(
            f"{entry.path} ({entry.state})"
            for entry in report.entries
            if entry.state in ("missing", "mismatch")
        )
        detail = problems or "the generation names none of the run's files"
        raise VoiceLabError(
            f"the vault's copy of {run_root} did not verify, so nothing was archived or "
            f"removed: {detail}"
        )
    return report


def _finetune_archived_files(entries: list[Any], run_root: str) -> tuple[Any, ...]:
    from .finetune import FineTuneArchivedFile

    return tuple(
        FineTuneArchivedFile(
            path=entry.path.removeprefix(f"{run_root}/"),
            sha256=entry.sha256,
            size=entry.size,
            kind=entry.kind,
        )
        for entry in entries
    )


def _finetune_archive(options: argparse.Namespace) -> int:
    """Copy one finished run to the vault, verify it there, and record that it is archived."""
    from .finetune import (
        FineTuneRunArchive,
        read_run_archive,
        require_finished_run,
        write_run_archive,
    )
    from .library_generations import VaultRecords

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    _refuse_during_app_gpu_work(layout)
    run_root = _finetune_run_root(options)
    require_finished_run(layout, run_root, options.run)
    vault = _vault_for_training(layout)
    records = VaultRecords(layout)
    pushed = _finetune_push(vault, layout)
    _finetune_verify_run(vault, layout, run_root, pushed)
    archive = read_run_archive(layout, run_root)
    written: str | None = None
    if archive is None:
        entries = _finetune_run_entries(records, pushed.number, pushed.manifest_sha256, run_root)
        archive = FineTuneRunArchive(
            run_id=options.run,
            library_id=vault.library_id,
            destination_sha256=vault.destination.digest,
            generation=pushed.number,
            manifest_sha256=pushed.manifest_sha256,
            archived_at=utc_now(),
            files=_finetune_archived_files(entries, run_root),
        )
        written = str(write_run_archive(layout, run_root, archive)[0])
        # The record itself belongs in the vault, so a restore of the newest generation
        # brings back a run that says it is archived.
        pushed = _finetune_push(vault, layout)
    document: dict[str, JsonValue] = {
        "run_id": options.run,
        "run_root": run_root,
        "archived_at": archive.archived_at.isoformat(),
        "archive_record": written,
        "generation": pushed.number,
        "manifest_sha256": pushed.manifest_sha256,
        "verified_files": len(archive.files),
        "removed": None,
    }
    if options.remove_local_copy:
        document["removed"] = _finetune_remove_local_copy(
            options, vault, layout, records, run_root, pushed
        )
    _json_print(document)
    return 0


def _finetune_remove_local_copy(
    options: argparse.Namespace,
    vault: LibraryVault,
    layout: AssetLayout,
    records: Any,
    run_root: str,
    pushed: _Generation,
) -> JsonValue:
    """Remove the run's checkpoints, after a fresh verify and with the record written first.

    The app's fine-tune job and Speak request are asked again here. The check the command
    opened with is minutes old by now — a whole-library push and two whole-run verifies (for a
    real run, gigabytes of upload, download and decryption) happened in between — and a Speak
    request that started in that window resolved a run whose checkpoint is about to go.
    Both questions are two metadata reads.
    """
    from .finetune import (
        FineTuneRunRemoval,
        read_run_removal,
        removable_run_files,
        remove_run_files,
        write_run_removal,
    )

    removal = read_run_removal(layout, run_root)
    record: str | None = None
    if removal is None:
        _finetune_verify_run(vault, layout, run_root, pushed)
        entries = _finetune_run_entries(records, pushed.number, pushed.manifest_sha256, run_root)
        files = removable_run_files(_finetune_archived_files(entries, run_root))
        if not files:
            raise VoiceLabError(
                f"generation {pushed.number} holds no checkpoint of {run_root}; there is "
                "nothing whose local copy could be removed"
            )
        _refuse_during_app_gpu_work(layout)
        removal = FineTuneRunRemoval(
            run_id=options.run,
            generation=pushed.number,
            manifest_sha256=pushed.manifest_sha256,
            removed_at=utc_now(),
            files=files,
        )
        record = str(write_run_removal(layout, run_root, removal)[0])
    _refuse_during_app_gpu_work(layout)
    removed = remove_run_files(layout, run_root, removal.files)
    return {
        "removal_record": record,
        "generation": removal.generation,
        "manifest_sha256": removal.manifest_sha256,
        "files": list(removed),
        "freed_bytes": sum(
            file.size for file in removal.files if file.path in set(removed) and file.kind == "file"
        ),
    }


def _finetune_restored_already(
    options: argparse.Namespace, layout: AssetLayout, run_root: str, removal: Any
) -> int:
    """Finish a restore whose files are all back already: keep the record as history."""
    from .finetune import keep_removal_history

    history = keep_removal_history(layout, run_root)
    _json_print(
        {
            "run_id": options.run,
            "run_root": run_root,
            "generation": removal.generation,
            "manifest_sha256": removal.manifest_sha256,
            "restored": [
                {"path": f"{run_root}/{file.path}", "size": file.size, "state": "kept"}
                for file in removal.files
                if file.kind == "file"
            ],
            "restored_bytes": 0,
            "pins": [
                {"path": f"{run_root}/{file.path}", "state": "linked", "reason": None}
                for file in removal.files
                if file.kind == "pin"
            ],
            "removal_history": str(history),
        }
    )
    return 0


def _finetune_restore_run(options: argparse.Namespace) -> int:
    """Bring an archived run's removed files back from the vault, into the same run directory.

    Without ``--runtime-root`` the pinned base weights are reported ``missing`` rather than
    refused, exactly as ``voice-lab vault restore`` reports them. Nothing in the lab reads
    that hard link after training — Speak and ``finetune synthesize`` read ``model_last.pt``,
    and only F5's trainer seeds or resumes from the pin — so a restore that brings back
    everything Speak needs is a success, and the run goes back to ``local``. The runtime lives
    outside the library and is not restored with it, so on a Mac rebuilt from the vault there
    is often no runtime to link from yet; refusing there would leave the checkpoints
    unrestorable for the one thing they are wanted for. Naming a runtime asks for the link,
    and a runtime that does not hold the pinned bytes is then refused by name.
    """
    from .finetune import (
        FineTuneError,
        keep_removal_history,
        read_run_removal,
        require_finished_run,
        run_files_are_the_recorded_ones,
    )
    from .library_generations import VaultRecords, restore_run_files

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    _refuse_during_app_gpu_work(layout)
    run_root = _finetune_run_root(options)
    require_finished_run(layout, run_root, options.run)
    removal = read_run_removal(layout, run_root)
    if removal is None:
        raise VoiceLabError(
            f"fine-tune run {options.run} has no {run_root}/local-removal.json; nothing of it "
            "was removed from this Mac"
        )
    if run_files_are_the_recorded_ones(layout, run_root, removal.files):
        # Nothing left this run: a whole-library restore wrote its files back beside the
        # record it also carries, or a crash landed before the first unlink. No vault is
        # needed (a Mac restored from the vault has none bound yet) and none is opened; the
        # record is kept as history, which is what leaves the run local again.
        return _finetune_restored_already(options, layout, run_root, removal)
    vault = _open_bound_vault(layout)
    records = VaultRecords(layout)
    entries = _finetune_run_entries(records, removal.generation, removal.manifest_sha256, run_root)
    wanted = {f"{run_root}/{file.path}": file for file in removal.files}
    missing = sorted(wanted.keys() - {entry.path for entry in entries})
    if missing:
        raise VoiceLabError(
            f"generation {removal.generation} does not hold {', '.join(missing)}, so the run "
            "cannot be restored from it"
        )
    runtime_root = None if options.runtime_root is None else Path(options.runtime_root)
    result = restore_run_files(
        layout.root,
        blob_store=vault.blob_store,
        entries=[entry for entry in entries if entry.path in wanted],
        runtime_root=runtime_root,
    )
    # A pin is only expected to land when a runtime was named to re-link it from; see the
    # docstring for why a restore without one reports the pin rather than refusing.
    if runtime_root is not None and result.pin_missing:
        raise FineTuneError(
            "FINETUNE.PRETRAINED_MISMATCH",
            f"{runtime_root} does not hold the weights {run_root}/run.json pins: "
            + "; ".join(f"{pin.path} ({pin.reason})" for pin in result.pin_missing),
        )
    history = keep_removal_history(layout, run_root)
    _json_print(
        {
            "run_id": options.run,
            "run_root": run_root,
            "generation": removal.generation,
            "manifest_sha256": removal.manifest_sha256,
            "restored": [
                {"path": file.path, "size": file.size, "state": file.state} for file in result.files
            ],
            "restored_bytes": result.written_bytes,
            "pins": [
                {"path": pin.path, "state": pin.state, "reason": pin.reason} for pin in result.pins
            ],
            "removal_history": str(history),
        }
    )
    return 0


def _prompts_coverage_report(options: argparse.Namespace) -> int:
    from .prompt_coverage import PromptCoverageProjectionError, prompt_coverage_report

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    # Built-ins resolve without the store; a published version is resolved and verified
    # through the store, which is closed again before any evidence is read.
    catalog = _resolve_catalog(options.catalog, options.asset_root, BUILTIN_CATALOG_RESOURCES)
    baseline = (
        None
        if options.baseline is None
        else _resolve_catalog(options.baseline, options.asset_root, BUILTIN_CATALOG_RESOURCES)
    )
    try:
        report = prompt_coverage_report(
            layout,
            options.subject_id,
            catalog.catalog,
            None if baseline is None else baseline.catalog,
            reference=catalog.reference,
            baseline_reference=None if baseline is None else baseline.reference,
        )
    except PromptCoverageProjectionError as error:
        print(f"voice-lab: {_error_text(error)}", file=sys.stderr)
        return 1
    if options.json:
        _json_print(report.to_json())
    else:
        version = (
            "" if report.catalog_id is None else f"{report.catalog_id}@{report.catalog_version} "
        )
        print(
            f"{report.covered_prompt_count}/{report.total_prompt_count} prompts covered · "
            f"catalog {version}{report.catalog_sha256}"
        )
        for label, values in (
            ("missing", report.missing_prompt_ids),
            ("changed", report.changed_prompt_ids),
            ("lost", report.lost_prompt_ids),
            ("unknown", report.unknown_record_ids),
            ("baseline unknown", report.baseline_unknown_record_ids),
        ):
            print(f"{label}: {', '.join(values) or '-'}")
    return report.exit_status


# -- voice-lab catalog: the corpus store (provenance/corpus) --------------------------------


def _catalog_layout(options: argparse.Namespace) -> AssetLayout:
    return AssetLayout.validate(_asset_root(options.asset_root))


def _new_catalog_id(catalog_id: str) -> str:
    """A new catalog needs a stable id that the CLI does not resolve to a built-in first."""
    if not _is_stable_id(catalog_id):
        from .catalog_ports import CorpusStoreError

        raise CorpusStoreError(
            "CORPUS.CATALOG_INVALID",
            f"catalog id {catalog_id!r} is not a stable id (lowercase letters, digits, '.', "
            "'_' and '-')",
        )
    if catalog_id in PROMPT_SHOW_RESOURCES or catalog_id in BUILTIN_CATALOG_RESOURCES:
        raise CatalogCommandError(
            "CATALOG_CLI.CATALOG_ID_RESERVED",
            f"catalog id {catalog_id} is the name of a released built-in prompt suite; "
            "choose another id",
        )
    return catalog_id


def _timestamp_json(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _prompt_record_json(record: PromptRecord) -> dict[str, JsonValue]:
    reference = record.source_reference
    return {
        "prompt_id": record.prompt.prompt_id,
        "revision": record.revision,
        "prompt_sha256": record.prompt_sha256,
        "prompt": record.prompt.model_dump(mode="json"),
        "keeps_recordings": record.keeps_recordings,
        "accepted_prompt_sha256s": list(record.accepted_prompt_sha256s),
        "source": record.source,
        "source_reference": None if reference is None else json.loads(reference),
        "created_at": _timestamp_json(record.created_at),
        "revision_created_at": _timestamp_json(record.revision_created_at),
        "retired_at": _timestamp_json(record.retired_at),
    }


def _catalog_record_json(record: CatalogRecord) -> dict[str, JsonValue]:
    return {
        "catalog_id": record.catalog_id,
        "suite_id": record.suite_id,
        "purpose": record.purpose,
        "description": record.description,
        "created_at": _timestamp_json(record.created_at),
        "retired_at": _timestamp_json(record.retired_at),
        "versions": [version.model_dump(mode="json") for version in record.versions],
    }


def _open_catalog_reader(layout: AssetLayout) -> CatalogReader:
    from .storage_binding import open_catalog_reader

    return open_catalog_reader(layout)


def _open_catalog_editor(layout: AssetLayout, *, create: bool = False) -> CatalogEditor:
    from .storage_binding import open_catalog_editor

    return open_catalog_editor(layout, create=create)


def _published(
    store: CatalogReader, catalog_id: str, version: int | None
) -> tuple[CatalogReference, CatalogSnapshot]:
    reference = store.resolve_reference(catalog_id, version)
    return reference, store.materialize(reference)


def _active_reference(store: CatalogReader, subject_id: str) -> CatalogReference | None:
    """The subject's active version, or ``None``; a damaged active snapshot still fails."""
    from .catalog_ports import CorpusStoreError

    try:
        return store.active_catalog(subject_id)[1]
    except CorpusStoreError as error:
        if error.code != "CORPUS.NO_ACTIVE_CATALOG":
            raise
        return None


def _init_refusal(store: CatalogReader, subject_id: str) -> str | None:
    """Why ``catalog init`` stops on this store, with the command that continues, or None."""
    prompts = store.list_prompts(include_retired=True)
    catalogs = store.list_catalogs()
    if not prompts and not catalogs:
        return None
    published = [record for record in catalogs if record.versions]
    activate = f"`voice-lab catalog activate --subject-id {subject_id} --catalog-id"
    if not catalogs:
        step = (
            "create a catalog for them (`voice-lab catalog create <catalog_id> --purpose "
            "<purpose>`), publish it (`voice-lab catalog publish --catalog-id <catalog_id>`) "
            f"and activate it ({activate} <catalog_id> --version 1`)"
        )
    elif not published:
        first = catalogs[0].catalog_id
        step = (
            f"publish {first} (`voice-lab catalog publish --catalog-id {first}`) and activate "
            f"it ({activate} {first} --version 1`)"
        )
    elif (active := _active_reference(store, subject_id)) is None:
        latest = published[-1].versions[-1]
        step = (
            f"activate a published version ({activate} {latest.catalog_id} "
            f"--version {latest.catalog_version}`)"
        )
    else:
        step = (
            f"nothing; {subject_id} already records against {active.catalog_id} version "
            f"{active.catalog_version}"
        )
    return (
        f"the corpus store already holds {len(prompts)} prompt(s) and {len(catalogs)} "
        f"catalog(s); `voice-lab catalog init` never seeds it again; next step: {step} "
        f"(inspect it with `voice-lab catalog list --subject-id {subject_id}`)"
    )


def _catalog_init(options: argparse.Namespace) -> int:
    from .catalog_ports import CorpusStoreError
    from .corpus_store import catalog_definition, validated_subject_id, validation_problems
    from .storage_binding import catalog_store_location

    layout = _catalog_layout(options)
    catalog_id = _new_catalog_id(options.catalog_id)
    subject_id = validated_subject_id(options.subject_id)
    builtin = (
        None
        if options.from_builtin is None
        else load_builtin_prompt_suite(BUILTIN_CATALOG_RESOURCES[options.from_builtin])
    )
    if builtin is None and options.purpose is None:
        raise CatalogCommandError(
            "CATALOG_CLI.USAGE_INVALID", "catalog init without --from-builtin requires --purpose"
        )
    if builtin is None and options.drop_room_tones:
        raise CatalogCommandError(
            "CATALOG_CLI.USAGE_INVALID", "--drop-room-tones applies only to a --from-builtin seed"
        )
    purpose = options.purpose or cast(PromptSuite, builtin).purpose
    # The root decides first: a physical-test catalog belongs on a marked root only, and a
    # marked root binds its one subject.
    require_root_purpose(
        layout, "physical-test" if purpose == "physical-test" else "standard", subject_id=subject_id
    )
    suite_id = options.suite_id
    if suite_id is None:
        suite_id = catalog_id
        if purpose == "physical-test" and not catalog_id.startswith(PHYSICAL_TEST_SUITE_PREFIX):
            if builtin is None or builtin.purpose != "physical-test":
                raise CatalogCommandError(
                    "CATALOG_CLI.USAGE_INVALID",
                    f"a physical-test catalog needs a suite id starting with "
                    f"{PHYSICAL_TEST_SUITE_PREFIX}; pass --suite-id "
                    f"{PHYSICAL_TEST_SUITE_PREFIX}<name> (or such a --catalog-id)",
                )
            suite_id = builtin.suite_id
    # Everything the store will hold is validated before the store is created.
    definition = catalog_definition(
        catalog_id, suite_id=suite_id, purpose=purpose, description=options.description
    )
    seed: PromptSuite | None = None
    dropped: list[str] = []
    if builtin is not None:
        kept = []
        for prompt in builtin.prompts:
            if options.drop_room_tones and prompt.category == "room-tone":
                dropped.append(prompt.prompt_id)
            else:
                kept.append(prompt)
        try:
            seed = PromptSuite(
                schema_version=PROMPT_SUITE_V2,
                suite_id=definition.suite_id,
                purpose=definition.purpose,
                prompts=tuple(kept),
            )
        except ValidationError as error:
            raise CorpusStoreError(
                "CORPUS.CATALOG_INVALID",
                f"{options.from_builtin} cannot seed catalog {catalog_id} as a "
                f"{purpose} prompt-suite/v2 catalog: {validation_problems(error)}",
            ) from error
        require_catalog_root(layout, seed, subject_id)
    with _open_catalog_editor(layout, create=True) as store:
        refusal = _init_refusal(store, subject_id)
        if refusal is not None:
            raise CorpusStoreError("CORPUS.CATALOG_DUPLICATE", refusal)
        # One store transaction: seed, catalog, version 1 and activation, or nothing.
        record, reference = store.initialize_catalog(
            definition,
            seed=seed,
            source=BUILTIN_PROMPT_SOURCES.get(options.from_builtin, "import"),
            subject_id=None if seed is None else subject_id,
        )
    _json_print(
        {
            "asset_root": str(layout.root),
            "store": catalog_store_location(layout),
            "catalog": _catalog_record_json(record),
            "seeded_from": options.from_builtin,
            "prompt_count": 0 if seed is None else len(seed.prompts),
            "dropped_prompt_ids": dropped,
            "reference": None if reference is None else reference.model_dump(mode="json"),
            "active_subject_id": None if reference is None else subject_id,
        }
    )
    return 0


def _catalog_create(options: argparse.Namespace) -> int:
    from .corpus_store import catalog_definition

    layout = _catalog_layout(options)
    definition = catalog_definition(
        _new_catalog_id(options.catalog_id),
        suite_id=options.suite_id,
        purpose=options.purpose,
        description=options.description,
    )
    with _open_catalog_editor(layout) as store:
        # The store refuses a purpose that cannot hold every live prompt.
        record = store.create_catalog(
            definition.catalog_id,
            suite_id=definition.suite_id,
            purpose=definition.purpose,
            description=definition.description,
        )
    _json_print(_catalog_record_json(record))
    return 0


def _catalog_list(options: argparse.Namespace) -> int:
    layout = _catalog_layout(options)
    active: CatalogReference | None = None
    with _open_catalog_reader(layout) as store:
        catalogs = store.list_catalogs()
        prompts = store.list_prompts(include_retired=True)
        if options.subject_id is not None:
            active = _active_reference(store, options.subject_id)
    retired = sum(record.retired_at is not None for record in prompts)
    if options.json:
        _json_print(
            {
                "catalogs": [_catalog_record_json(record) for record in catalogs],
                "live_prompt_count": len(prompts) - retired,
                "retired_prompt_count": retired,
                "subject_id": options.subject_id,
                "active": None if active is None else active.model_dump(mode="json"),
            }
        )
        return 0
    for record in catalogs:
        latest = record.versions[-1] if record.versions else None
        published = (
            "nothing published"
            if latest is None
            else f"latest version {latest.catalog_version} · {latest.catalog_sha256}"
        )
        print(f"{record.catalog_id} · suite {record.suite_id} · {record.purpose} · {published}")
        if record.description:
            print(f"  {record.description}")
    if not catalogs:
        print("no catalogs")
    print(f"prompts: {len(prompts) - retired} live · {retired} retired")
    if options.subject_id is not None:
        print(
            f"{options.subject_id} has no active catalog"
            if active is None
            else f"{options.subject_id} records against {active.catalog_id} "
            f"version {active.catalog_version}"
        )
    return 0


def _catalog_show(options: argparse.Namespace) -> int:
    layout = _catalog_layout(options)
    with _open_catalog_reader(layout) as store:
        reference, snapshot = _published(store, options.catalog_id, options.version)
    if options.json:
        _json_print(
            {
                "reference": reference.model_dump(mode="json"),
                "snapshot": snapshot.model_dump(mode="json"),
            }
        )
        return 0
    print(
        f"{reference.catalog_id} version {reference.catalog_version} · "
        f"{len(snapshot.prompt_aliases)} kept-recording aliases · catalog "
        f"{reference.catalog_sha256}"
    )
    _print_suite(snapshot.suite)
    return 0


def _catalog_export(options: argparse.Namespace) -> int:
    layout = _catalog_layout(options)
    with _open_catalog_reader(layout) as store:
        _reference, snapshot = _published(store, options.catalog_id, options.version)
    _json_print(snapshot.suite.model_dump(mode="json"))
    return 0


def _prompt_field_changes(options: argparse.Namespace) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if options.text is not None or options.no_text:
        changes["text"] = options.text
    if options.instructions is not None or options.no_instructions:
        changes["instructions"] = options.instructions
    for field in ("category", "style", "expected_seconds", "reference_candidate", "holdout"):
        if getattr(options, field) is not None:
            changes[field] = getattr(options, field)
    if options.label is not None or options.no_labels:
        changes["labels"] = tuple(options.label or ())
    return changes


def _catalog_add_prompt(options: argparse.Namespace) -> int:
    from .corpus_store import prompt_from_fields
    from .standard_prompt_sets import (
        STANDARD_PROMPT_CATEGORY,
        STANDARD_PROMPT_STYLE,
        standard_expected_seconds,
    )

    layout = _catalog_layout(options)
    expected_seconds = options.expected_seconds
    if expected_seconds is None:
        if options.text is None:
            raise CatalogCommandError(
                "CATALOG_CLI.USAGE_INVALID", "a prompt without --text needs --expected-seconds"
            )
        expected_seconds = standard_expected_seconds(options.text)
    fields: dict[str, Any] = {
        "text": options.text,
        "instructions": options.instructions,
        "category": options.category or STANDARD_PROMPT_CATEGORY,
        "style": options.style or STANDARD_PROMPT_STYLE,
        "expected_seconds": expected_seconds,
        "labels": tuple(options.label or ()),
        "reference_candidate": options.reference_candidate,
        "holdout": options.holdout,
    }
    # The fields are checked before the store is opened; the store checks them again, with
    # every rule about the other live prompts and the catalogs, inside its write.
    prompt_from_fields(options.prompt_id, **fields)
    with _open_catalog_editor(layout) as store:
        record = store.add_prompt(options.prompt_id, source="manual", **fields)
    _json_print(_prompt_record_json(record))
    return 0


def _quoted(text: str | None) -> str:
    return "(no spoken text)" if text is None else json.dumps(text, ensure_ascii=False)


def _text_change_line(prompt_id: str, before: Prompt, after: Prompt) -> str | None:
    """The spoken-text edit as the operator should see it before it is written."""
    from .corpus_store import MAX_KEPT_CHARACTER_EDITS, MAX_KEPT_WORD_EDITS, text_change

    if before.text == after.text:
        return None
    line = f"{prompt_id} text: {_quoted(before.text)} -> {_quoted(after.text)}"
    if before.text is None or after.text is None:
        return line
    change = text_change(before.text, after.text)
    if change.spoken_words_unchanged:
        kind = "case, punctuation or spacing only"
    elif change.typo_class:
        words, characters = change.word_edits, change.character_edits
        kind = (
            f"a typo-class fix: {words} word edit{'' if words == 1 else 's'}, "
            f"{characters} character edit{'' if characters == 1 else 's'}"
        )
    else:
        kind = (
            f"a rewording: more than {MAX_KEPT_WORD_EDITS} word edits or "
            f"{MAX_KEPT_CHARACTER_EDITS} character edits"
        )
    return f"{line} ({kind})"


def _text_change_json(before: Prompt, after: Prompt) -> dict[str, JsonValue] | None:
    from .corpus_store import text_change

    if before.text == after.text:
        return None
    change = (
        None if before.text is None or after.text is None else text_change(before.text, after.text)
    )
    return {
        "before": before.text,
        "after": after.text,
        "word_edits": None if change is None else change.word_edits,
        "character_edits": None if change is None else change.character_edits,
        "typo_class": change is not None and change.typo_class,
    }


def _recordings_line(prompt_id: str, subject_id: str, count: int, keep: bool) -> str:
    """What an edit does to the recordings that count for the prompt now, in one line."""
    if count == 0:
        return (
            f"{prompt_id}: no recordings of {subject_id} count for this prompt now, so "
            f"this edit {'keeps' if keep else 'invalidates'} none"
        )
    them = "it" if count == 1 else "them"
    consequence = (
        f"this edit keeps {them} (counted for the new text, which becomes the training transcript)"
        if keep
        else f"this edit invalidates {them} (not counted until {prompt_id} is recorded again)"
    )
    return (
        f"{prompt_id}: {count} recording{'' if count == 1 else 's'} of {subject_id} "
        f"count{'s' if count == 1 else ''} for this prompt now; {consequence}"
    )


def _catalog_edit_prompt(options: argparse.Namespace) -> int:
    from .prompt_coverage import prompt_recordings

    layout = _catalog_layout(options)
    changes = _prompt_field_changes(options)
    if not changes:
        raise CatalogCommandError(
            "CATALOG_CLI.USAGE_INVALID",
            "catalog edit-prompt needs a field to change (--text, --instructions, --category, "
            "--style, --expected-seconds, --label, --reference-candidate or --holdout)",
        )
    if options.allow_rewording and not options.keep_recordings:
        raise CatalogCommandError(
            "CATALOG_CLI.USAGE_INVALID", "--allow-rewording only qualifies --keep-recordings"
        )
    prompt_id = options.prompt_id
    keep = options.keep_recordings
    rewording = options.allow_rewording
    with _open_catalog_editor(layout) as store:
        current = store.prompt_history(prompt_id)[-1]
        # The revision counted and shown here is the only one the write may replace.
        revision = options.expected_revision or current.revision
        # Every store check (the keep-recordings rewording bound and catalog fit included)
        # runs first in a rolled-back preview, so a refused edit never reports recordings it
        # would not touch.
        preview = store.edit_prompt(
            prompt_id,
            keep_recordings=keep,
            allow_rewording=rewording,
            expected_revision=revision,
            dry_run=True,
            **changes,
        )
        counted = prompt_recordings(
            layout, options.subject_id, current.prompt, current.accepted_prompt_sha256s
        )
        count = len(counted.take_ids)
        lines = [
            line
            for line in (_text_change_line(prompt_id, current.prompt, preview.prompt),)
            if line is not None
        ]
        lines.append(_recordings_line(prompt_id, options.subject_id, count, keep))
        unknown = counted.unknown_record_ids
        if unknown:
            state = "record whose prompts" if len(unknown) == 1 else "records whose prompts"
            verb = "is" if len(unknown) == 1 else "are"
            lines.append(
                f"  {len(unknown)} {state} cannot be told {verb} not counted: {', '.join(unknown)}"
            )
        # Shown before anything is written; with --json stdout carries one document only.
        print("\n".join(lines), file=sys.stderr if options.json else sys.stdout, flush=True)
        record = (
            preview
            if options.dry_run
            else store.edit_prompt(
                prompt_id,
                keep_recordings=keep,
                allow_rewording=rewording,
                expected_revision=current.revision,
                **changes,
            )
        )
    if options.json:
        _json_print(
            {
                "prompt_id": prompt_id,
                "subject_id": options.subject_id,
                "keep_recordings": keep,
                "text_change": _text_change_json(current.prompt, preview.prompt),
                "recordings": {
                    "count": count,
                    "outcome": "kept" if keep else "invalidated",
                    "take_ids": list(counted.take_ids),
                    "unknown_record_ids": list(unknown),
                },
                "written": not options.dry_run,
                "previous": _prompt_record_json(current),
                "record": _prompt_record_json(record),
            }
        )
        return 0
    state = "would be" if options.dry_run else "is now"
    print(
        f"{prompt_id} {state} revision {record.revision} · {record.prompt_sha256} · "
        f"counts recordings of {len(record.accepted_prompt_sha256s)} earlier revision(s)"
    )
    print(
        f"not written (--dry-run; write it with --expected-revision {current.revision})"
        if options.dry_run
        else "publish a version (`voice-lab catalog publish`) and activate it for the app"
    )
    return 0


def _catalog_retire_prompt(options: argparse.Namespace) -> int:
    layout = _catalog_layout(options)
    with _open_catalog_editor(layout) as store:
        record = store.retire_prompt(options.prompt_id)
    _json_print(_prompt_record_json(record))
    return 0


def _require_standard_set_absent(store: CatalogReader, name: str, first_prompt_id: str) -> None:
    from .catalog_ports import CorpusStoreError

    try:
        store.prompt_history(first_prompt_id)
    except CorpusStoreError as error:
        if error.code == "CORPUS.PROMPT_UNKNOWN":
            return
        raise
    raise CorpusStoreError(
        "CORPUS.PROMPT_DUPLICATE",
        f"the store already holds {first_prompt_id}, the first sentence of the {name} set, so "
        "the set is imported already; nothing was read, fetched or written",
    )


def _catalog_import_standard(
    options: argparse.Namespace, name: Literal["harvard", "arctic"]
) -> int:
    from .standard_prompt_sets import (
        STANDARD_SET_FIRST_PROMPT_IDS,
        fetch_standard_set,
        read_standard_set_file,
        source_url,
        standard_prompt_set,
    )

    layout = _catalog_layout(options)
    if options.fetch and (options.fetched_at is not None or options.source_url is not None):
        raise CatalogCommandError(
            "CATALOG_CLI.USAGE_INVALID",
            "--fetched-at and --source-url describe a --from-file copy; --fetch records the "
            "time it fetches and the URL the list came from",
        )
    if not options.fetch and (options.fetched_at is None or options.source_url is None):
        raise CatalogCommandError(
            "CATALOG_CLI.USAGE_INVALID",
            "--from-file requires --fetched-at and --source-url (when and where the copy was "
            "downloaded; both are recorded as given)",
        )
    if not options.fetch:
        source_url(options.source_url)
    # An import that cannot succeed reads and downloads nothing: the store must exist and
    # must not hold the set yet.
    with _open_catalog_reader(layout) as store:
        _require_standard_set_absent(store, name, STANDARD_SET_FIRST_PROMPT_IDS[name])
    if options.fetch:
        payload, fetched_at, url = fetch_standard_set(name)
    else:
        payload = read_standard_set_file(Path(options.from_file))
        fetched_at, url = options.fetched_at, options.source_url
    prompt_set = standard_prompt_set(
        name,
        payload,
        fetched_at=fetched_at,
        url=url,
        accept_unpinned_source=options.accept_unpinned_source,
    )
    with _open_catalog_editor(layout) as store:
        # One store transaction: every catalog must hold the set, and no sentence may read
        # like a live prompt, or nothing is imported.
        prompt_ids = store.import_prompt_suite(
            prompt_set.suite, source=name, source_reference=prompt_set.source_reference
        )
    _json_print(
        {
            "source": name,
            "url": prompt_set.url,
            "fetched_at": prompt_set.fetched_at.isoformat(),
            "sha256": prompt_set.sha256,
            "pinned_sha256": prompt_set.pinned_sha256,
            "unpinned_source_accepted": prompt_set.unpinned_source_accepted,
            "prompt_count": len(prompt_ids),
            "first_prompt_id": prompt_ids[0],
            "last_prompt_id": prompt_ids[-1],
            "expected_seconds": round(
                sum(prompt.expected_seconds for prompt in prompt_set.suite.prompts), 1
            ),
        }
    )
    return 0


def _catalog_import_harvard(options: argparse.Namespace) -> int:
    return _catalog_import_standard(options, "harvard")


def _catalog_import_arctic(options: argparse.Namespace) -> int:
    return _catalog_import_standard(options, "arctic")


def _catalog_import_json(options: argparse.Namespace) -> int:
    layout = _catalog_layout(options)
    suite = _read_bounded_contract(Path(options.suite_path), PromptSuite)
    with _open_catalog_editor(layout) as store:
        prompt_ids = store.import_prompt_suite(suite, source="import")
    _json_print(
        {
            "suite_id": suite.suite_id,
            "suite_sha256": canonical_sha256(suite),
            "source": "import",
            "prompt_count": len(prompt_ids),
            "prompt_ids": list(prompt_ids),
        }
    )
    return 0


def _catalog_publish(options: argparse.Namespace) -> int:
    layout = _catalog_layout(options)
    with _open_catalog_editor(layout) as store:
        reference = store.publish_version(options.catalog_id)
        snapshot = store.materialize(reference)
    _json_print(
        {
            "reference": reference.model_dump(mode="json"),
            "prompt_count": len(snapshot.suite.prompts),
            "alias_count": len(snapshot.prompt_aliases),
            "expected_seconds": round(
                sum(prompt.expected_seconds for prompt in snapshot.suite.prompts), 1
            ),
        }
    )
    return 0


def _catalog_activate(options: argparse.Namespace) -> int:
    layout = _catalog_layout(options)
    with _open_catalog_editor(layout) as store:
        reference, snapshot = _published(store, options.catalog_id, options.version)
        require_catalog_root(layout, snapshot.suite, options.subject_id)
        store.activate(options.subject_id, reference)
    _json_print(
        {
            "subject_id": options.subject_id,
            "reference": reference.model_dump(mode="json"),
            "prompt_count": len(snapshot.suite.prompts),
            "takes_effect": "at the next launch of the recording app",
        }
    )
    return 0


def _capture_devices(options: argparse.Namespace) -> int:
    devices = list_avfoundation_audio_devices(ffmpeg=options.ffmpeg)
    _json_print(
        {
            "devices": [
                {
                    "device_id": device.device_id,
                    "display_name": device.display_name,
                    "is_umik1": "umik-1" in device.display_name.casefold(),
                }
                for device in devices
            ]
        }
    )
    return 0


def _capture_run(options: argparse.Namespace) -> int:
    layout, suite, device = _capture_inputs(options)
    result = capture_prompt_suite(
        layout=layout,
        prompt_suite=suite,
        subject_id=options.subject_id,
        device=device,
        session_id=options.session_id,
        ffmpeg=options.ffmpeg,
    )
    _json_print(
        {
            "session_directory": str(result.session_directory),
            "manifest_sha256": result.manifest_sha256,
            "qc_report_sha256": result.qc_report_sha256,
            "qc_status": result.qc_report.status,
            "ready": result.ready.model_dump(mode="json"),
        }
    )
    return 0


def _capture_inputs(
    options: argparse.Namespace,
) -> tuple[AssetLayout, PromptSuite, CaptureDevice]:
    layout = require_legacy_capture_root(AssetLayout.validate(_asset_root(options.asset_root)))
    suite = require_legacy_capture_suite(load_prompt_suite(options.suite))
    selected = select_umik1_device(
        list_avfoundation_audio_devices(ffmpeg=options.ffmpeg),
        requested=options.device,
    )
    device = CaptureDevice(
        device_id=selected.device_id,
        display_name=selected.display_name,
        microphone_serial=options.microphone_serial,
    )
    return layout, suite, device


def _capture_console(options: argparse.Namespace) -> int:
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        from .capture_console import serve_capture_console

        layout, suite, device = _capture_inputs(options)
        engine = TakeEngine(
            layout=layout,
            prompt_suite=suite,
            subject_id=options.subject_id,
            device=device,
            session_id=options.session_id,
            ffmpeg=options.ffmpeg,
            resume=options.resume,
        )
        serve_capture_console(engine, open_browser=options.open)
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode
    return 0


def _desktop(options: argparse.Namespace) -> int:
    previous_dont_write_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        from .desktop import run_desktop

        return run_desktop(
            asset_root=_asset_root(options.asset_root),
            suite=options.suite,
            subject_id=options.subject_id,
            device=options.device,
            ffmpeg=options.ffmpeg,
            pilot_gate_path=options.pilot_gate_path,
            pilot_gate_sha256=options.pilot_gate_sha256,
        )
    finally:
        sys.dont_write_bytecode = previous_dont_write_bytecode


def _recording_continue_legacy(options: argparse.Namespace) -> int:
    """Continue an unfinished desktop session in a new native context.

    The old session's own frozen evidence supplies the subject and catalog, so the
    operator names only the new acoustic setup. The catalog stays embedded in the context
    (the one open-context form that still embeds one): a desktop session binds a frozen
    catalog that no corpus-store version holds. Original dates, devices and takes are
    preserved; no legacy raw audio is rewritten and no engine is reopened.
    Raises: ``ContractError`` for a finalized session or evidence that no longer binds the
    session; ``SecurityError`` for unsafe or changed evidence (as
    ``RecordingRecovery.continue_legacy``).
    """

    import hashlib
    import uuid

    from .canonical import canonical_json_bytes
    from .errors import ContractError
    from .models import CaptureDevice
    from .paths import AssetLayout, confined_path
    from .recording_contracts import ContinueLegacyRecordingCommand, OpenContextCommand
    from .recording_library import _read_sidecar
    from .recording_recovery import RecordingRecovery
    from .recording_takes import RecordingTakeStore

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    sidecar = confined_path(
        layout.root, f"provenance/capture-desktop/{options.session_id}.json", must_exist=True
    )
    evidence = _read_sidecar(sidecar)
    if evidence.manifest_sha256 is not None:
        raise ContractError("this desktop session is already finalized; nothing to continue")
    digest = hashlib.sha256(canonical_json_bytes(evidence) + b"\n").hexdigest()
    operation_id = f"continue-legacy-{uuid.uuid4().hex}"
    command = ContinueLegacyRecordingCommand(
        operation_id=operation_id,
        session_id=evidence.session_id,
        session_evidence_sha256=digest,
        context=OpenContextCommand(
            operation_id=operation_id,
            subject_id=evidence.subject_id,
            catalog=evidence.catalog_suite,
            catalog_sha256=evidence.catalog_sha256,
            capture_device=CaptureDevice(
                backend="coreaudio",
                device_id=options.device_id,
                display_name=options.display_name,
            ),
            capture_host=options.capture_host,
            new_setup=True,
            setup_description=options.setup_description,
        ),
    )
    store = RecordingTakeStore(layout)
    event = RecordingRecovery(store, subject_id=evidence.subject_id).continue_legacy(command)
    print(
        json.dumps(
            {
                "continued_session_id": evidence.session_id,
                "group_id": event.group.group_id,
                "subject_id": event.group.subject_id,
                "catalog_sha256": event.group.catalog_sha256,
                "operation_id": operation_id,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _recording_service(options: argparse.Namespace) -> int:
    """Forward to ``recording_service.main``; ``--catalog-sha256`` only when it was given.

    A workspace service serves its subject's active corpus-store catalog (the parser refuses
    ``--catalog-sha256`` with ``--native-workspace``); group mode still names its group's
    digest. Returns the service's exit status (errors as that ``main``).
    """
    from .recording_service import main as recording_service_main

    return recording_service_main(
        [
            "--asset-root",
            str(_asset_root(options.asset_root)),
        ]
        + (
            ["--catalog-sha256", options.catalog_sha256]
            if options.catalog_sha256 is not None
            else []
        )
        + [
            "--process-generation",
            options.process_generation,
            "--build-identity",
            options.build_identity,
            "--expected-client-build",
            options.expected_client_build,
        ]
        + (
            ["--native-workspace", "--subject-id", options.subject_id]
            if options.native_workspace
            else []
        )
        + (
            ["--recording-group-id", options.recording_group_id]
            if options.recording_group_id is not None
            else []
        )
    )


def _intake_validate(options: argparse.Namespace) -> int:
    package = validate_capture_package(Path(options.session).expanduser())
    _json_print(
        {
            "session_id": package.manifest.session_id,
            "manifest_sha256": package.manifest_sha256,
            "qc_report_sha256": package.qc_report_sha256,
            "qc_status": package.qc_report.status,
        }
    )
    return 0


def _intake_accept(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    source = layout.directory("incoming") / options.session_id
    receipt = accept_capture_session(layout, source)
    _json_print(receipt.model_dump(mode="json"))
    return 0


def _intake_scan(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    accepted: list[object] = []
    errors: list[dict[str, str]] = []
    for candidate in sorted(layout.directory("incoming").iterdir()):
        if candidate.name.startswith("."):
            continue
        try:
            receipt = accept_capture_session(layout, candidate)
        except (VoiceLabError, ValidationError, OSError, ValueError) as error:
            errors.append({"session": candidate.name, "error": str(error)})
        else:
            accepted.append(receipt.model_dump(mode="json"))
    _json_print({"accepted": accepted, "errors": errors})
    return 1 if errors else 0


def _sync_plan(options: argparse.Namespace) -> int:
    plan = build_sync_plan(
        Path(options.session).expanduser(),
        target=options.target,
        remote_user=options.remote_user,
        remote_root=options.remote_root,
        identity_file=(Path(options.identity_file).expanduser() if options.identity_file else None),
        known_hosts_file=(
            Path(options.known_hosts_file).expanduser() if options.known_hosts_file else None
        ),
        dry_run=not options.live_plan,
    )
    if options.json:
        _json_print(
            {
                "session_id": plan.session_id,
                "source_directory": str(plan.source_directory),
                "target": plan.target,
                "remote_directory": plan.remote_directory,
                "dry_run": plan.dry_run,
                "plan_sha256": plan.approval_sha256(),
                "package_snapshot_sha256": plan.package_snapshot.sha256(),
                "package_snapshot": plan.package_snapshot.approval_payload(),
                "passes": [{"name": item.name, "argv": list(item.argv)} for item in plan.passes],
            }
        )
    else:
        print(plan.shell_display())
    if options.execute:
        execute_sync_plan(
            plan,
            allow_network=options.allow_network,
            approved_plan_sha256=options.approved_plan_sha256,
        )
    return 0


def _reference_review(options: argparse.Namespace) -> int:
    if not (
        options.headphones_used and options.transcript_matches and options.audio_quality_passed
    ):
        raise VoiceLabError(
            "reference review requires explicit headphone, transcript, "
            "and audio-quality attestations"
        )
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    source = load_accepted_utterance(
        layout,
        raw_manifest_sha256=options.raw_manifest_sha256,
        prompt_id=options.prompt_id,
    )
    record = HeadphoneReviewEvidence(
        review_id=options.review_id,
        reviewer_id=options.reviewer_id,
        session_id=source.binding.session_id,
        prompt_id=source.binding.prompt_id,
        source_sha256=source.binding.raw_audio_sha256,
        reviewed_at=datetime.now().astimezone(),
        headphones_used=True,
        transcript_matches=True,
        audio_quality_passed=True,
        findings=tuple(options.finding),
    )
    directory = layout.directory("provenance") / "headphone-reviews"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise VoiceLabError("headphone-review directory is unsafe")
    path = directory / f"{record.review_id}.json"
    if path.exists() or path.is_symlink():
        raise VoiceLabError("refusing to replace existing headphone-review evidence")
    digest = write_canonical_json(path, record)
    _json_print(
        {
            "path": path.relative_to(layout.root).as_posix(),
            "sha256": digest,
            "record": record.model_dump(mode="json"),
        }
    )
    return 0


def _reference_publish(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    published = publish_reference(
        layout,
        reference_id=options.reference_id,
        raw_manifest_sha256=options.raw_manifest_sha256,
        prompt_id=options.prompt_id,
        consent_path=options.consent_path,
        consent_sha256=options.consent_sha256,
        headphone_review_path=options.headphone_review_path,
        headphone_review_sha256=options.headphone_review_sha256,
        created_by=options.created_by,
        provenance_statement=options.provenance_statement,
    )
    _json_print(
        {
            "directory": str(published.directory),
            "manifest_sha256": published.manifest_sha256,
            "manifest": published.manifest.model_dump(mode="json"),
        }
    )
    return 0


def _reference_validate(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    published = validate_reference_publication(
        layout,
        layout.directory("references") / options.reference_id,
    )
    _json_print(
        {
            "directory": str(published.directory),
            "manifest_sha256": published.manifest_sha256,
            "reference_sha256": published.manifest.output.sha256,
            "transcript": published.manifest.source.transcript,
        }
    )
    return 0


def _dataset_pilot_gate(options: argparse.Namespace) -> int:
    if not (options.approve and options.headphone_review_completed and options.allow_full_corpus):
        raise VoiceLabError(
            "pilot-gate evidence requires --approve, --headphone-review-completed, "
            "and --allow-full-corpus"
        )
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    record = PilotGateApproval(
        gate_id=options.gate_id,
        pilot_manifest_sha256s=tuple(options.pilot_manifest_sha256),
        approved=True,
        headphone_review_completed=True,
        full_corpus_capture_allowed=True,
        approved_at=options.approved_at or datetime.now().astimezone(),
        approved_by=options.approved_by,
        statement=options.statement,
    )
    for manifest_sha256 in record.pilot_manifest_sha256s:
        package = validate_capture_package(
            layout.directory("raw") / manifest_sha256,
            allow_intake_receipt=True,
        )
        if package.manifest_sha256 != manifest_sha256:
            raise VoiceLabError("pilot gate names a non-content-addressed raw session")
        if package.qc_report.status != "pass":
            raise VoiceLabError("pilot-gate source capture QC status must be pass")
        # Refuse before the permanent approval below can be written.
        require_corpus_eligible_package(layout, package.manifest)

    directory = layout.directory("provenance") / "pilot-gates"
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise VoiceLabError("pilot-gate directory is unsafe")
    os.chmod(directory, 0o700)
    path = directory / f"{record.gate_id}.json"
    if path.exists() or path.is_symlink():
        raise VoiceLabError("refusing to replace existing pilot-gate evidence")
    digest = write_canonical_json(path, record, mode=0o400)
    relative_path = path.relative_to(layout.root).as_posix()
    bound = load_pilot_gate(
        layout,
        relative_path=relative_path,
        expected_sha256=digest,
    )
    _json_print(
        {
            "path": relative_path,
            "sha256": digest,
            "record": bound.record.model_dump(mode="json"),
        }
    )
    return 0


def _dataset_build(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    selection_document = _read_bounded_contract(
        Path(options.selection_file), DatasetSelectionDocument
    )
    prompt_suites = _prompt_suite_mappings(options.prompt_suite)
    manifest = build_dataset_manifest(
        layout,
        dataset_id=options.dataset_id,
        corpus_stage=options.corpus_stage,
        selections=selection_document.selections,
        prompt_suites=prompt_suites,
        consent_path=options.consent_path,
        consent_sha256=options.consent_sha256,
        created_by=options.created_by,
        minimum_holdout_seconds=options.minimum_holdout_seconds,
        pilot_gate_path=options.pilot_gate_path,
        pilot_gate_sha256=options.pilot_gate_sha256,
    )
    published = publish_dataset_manifest(layout, manifest)
    _json_print(
        {
            "directory": str(published.directory),
            "manifest_sha256": published.manifest_sha256,
            "manifest": published.manifest.model_dump(mode="json"),
        }
    )
    return 0


def _dataset_validate(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    published = validate_published_dataset(
        layout,
        layout.directory("derived") / "datasets" / options.dataset_id,
    )
    _json_print(
        {
            "directory": str(published.directory),
            "manifest_sha256": published.manifest_sha256,
            "manifest": published.manifest.model_dump(mode="json"),
        }
    )
    return 0


def _dataset_transfer_plan(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    plan = build_transfer_plan(
        layout,
        plan_id=options.plan_id,
        dataset_id=options.dataset_id,
        created_by=options.created_by,
        destination=options.hosted_destination,
        approval_path=options.hosted_approval_path,
        approval_sha256=options.hosted_approval_sha256,
    )
    published = publish_transfer_plan(layout, plan)
    _json_print(
        {
            "directory": str(published.directory),
            "plan_sha256": published.plan_sha256,
            "plan": published.plan.model_dump(mode="json"),
            "network_executed": False,
        }
    )
    return 0


def _models_list(options: argparse.Namespace) -> int:
    registry = _registry(options)
    _json_print(
        {
            "candidates": [
                candidate.model_dump(mode="json")
                for candidate in registry.candidates(track=options.track)
            ]
        }
    )
    return 0


def _models_verify_registry(options: argparse.Namespace) -> int:
    registry = _registry(options)
    registry.verify_requirements(_repository_root())
    _json_print(
        {
            "status": "pass",
            "candidate_count": len(registry.candidates()),
            "lock_resolved_at": registry.lock.resolved_at,
            "network_used": False,
        }
    )
    return 0


def _models_prepare(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    registry = _registry(options)
    registry.verify_requirements(_repository_root())
    resolved = registry.resolve(options.candidate_id, options.profile)
    base_python = Path(
        options.base_python or f"/opt/homebrew/bin/python{resolved.runtime.python_version}"
    )
    base_python_available = base_python.is_file() and os.access(base_python, os.X_OK)
    environment_path = layout.directory("runtimes") / resolved.runtime.runtime_id
    runtime_plan = build_runtime_install_plan(
        resolved.runtime,
        environment_path,
        base_python=base_python,
        repository_root=_repository_root(),
        reviewed_wheelhouse=(
            Path(options.reviewed_wheelhouse) if options.reviewed_wheelhouse else None
        ),
        lab_wheel=Path(options.lab_wheel) if options.lab_wheel else None,
        lab_wheel_sha256=options.lab_wheel_sha256,
    )
    runtime_python = environment_path / "bin/python"
    executable_code_sources = (
        (resolved.code_source,) if resolved.candidate.engine == "seed-vc" else ()
    )
    sources = (
        *executable_code_sources,
        resolved.model_source,
        *resolved.auxiliary_sources,
    )
    unique_sources = {source.source_id: source for source in sources}
    source_plans = []
    for source in unique_sources.values():
        destination = (
            layout.directory("runtimes") / "source-checkouts" / source.source_id
            if source.source_id == resolved.code_source.source_id
            else layout.directory("models") / "sources" / source.source_id
        )
        source_plans.append(
            build_source_provision_plan(
                source,
                destination,
                runtime_python=runtime_python,
            )
        )
    runtime_receipt_path: Path | None = None
    runtime_receipt_sha256: str | None = None
    if options.execute:
        if not base_python_available:
            raise VoiceLabError(f"required base Python is unavailable: {base_python}")
        if environment_path.exists():
            if not runtime_python.is_file():
                raise VoiceLabError("existing runtime is incomplete; refusing to overwrite it")
            if options.expected_runtime_receipt_sha256 is None:
                raise VoiceLabError(
                    "reusing an installed runtime requires --expected-runtime-receipt-sha256"
                )
            _, runtime_receipt_sha256 = verify_runtime_install_receipt(
                resolved.runtime,
                environment_path,
                repository_root=_repository_root(),
                expected_receipt_sha256=options.expected_runtime_receipt_sha256,
            )
            runtime_receipt_path = environment_path / ".cole-voice-lab-runtime-install.json"
        else:
            runtime_receipt_path, runtime_receipt_sha256 = execute_runtime_install_plan(
                runtime_plan,
                allow_downloads=options.allow_downloads,
            )
        for plan in source_plans:
            if plan.destination.exists():
                verify_local_source(plan.source, plan.destination)
            else:
                execute_provision_plan(
                    plan,
                    allow_downloads=options.allow_downloads,
                )
            verify_runtime_install_receipt(
                resolved.runtime,
                environment_path,
                repository_root=_repository_root(),
                expected_receipt_sha256=runtime_receipt_sha256,
            )
    _json_print(
        {
            "candidate_id": resolved.candidate.candidate_id,
            "profile": resolved.profile,
            "base_python": str(base_python),
            "base_python_available": base_python_available,
            "runtime": {
                "destination": str(environment_path),
                "commands": [list(command) for command in plan_commands(runtime_plan)],
                "target_platform": resolved.runtime.target_platform,
                "reviewed_wheelhouse": (
                    str(runtime_plan.reviewed_wheelhouse)
                    if runtime_plan.reviewed_wheelhouse is not None
                    else None
                ),
                "reviewed_wheels": [
                    {
                        "filename": item.wheel_filename,
                        "sha256": item.wheel_sha256,
                        "size_bytes": item.wheel_size_bytes,
                    }
                    for item in resolved.runtime.reviewed_built_wheels
                ],
                "lab_wheel": (
                    str(runtime_plan.lab_wheel) if runtime_plan.lab_wheel is not None else None
                ),
                "lab_wheel_sha256": runtime_plan.lab_wheel_sha256,
                "estimated_install_bytes": resolved.runtime.estimated_install_bytes,
                "receipt_path": str(runtime_receipt_path) if runtime_receipt_path else None,
                "receipt_sha256": runtime_receipt_sha256,
                "install_ready": runtime_plan.lab_wheel is not None
                and (
                    not resolved.runtime.reviewed_built_wheels
                    or runtime_plan.reviewed_wheelhouse is not None
                ),
            },
            "sources": [
                {
                    "source_id": plan.source.source_id,
                    "destination": str(plan.destination),
                    "commands": [list(command) for command in plan_commands(plan)],
                }
                for plan in source_plans
            ],
            "execution_requested": options.execute,
            "downloads_allowed": options.allow_downloads,
        }
    )
    return 0


def _models_evict(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    registry = _registry(options)
    source = registry.source(options.source_id)
    boundary = layout.directory("models") / "sources"
    plan = build_source_eviction_plan(
        source,
        boundary / source.source_id,
        boundary=boundary,
    )
    result: dict[str, object] = {
        "source_id": plan.source_id,
        "path": str(plan.path),
        "manifest_sha256": plan.manifest_sha256,
        "estimated_bytes": plan.estimated_bytes,
        "execution_requested": options.execute,
        "network_used": False,
        "irreversible": True,
    }
    if not options.execute:
        result["removed"] = False
        _json_print(result)
        return 0
    if not options.confirm_evict:
        raise VoiceLabError("model eviction requires --confirm-evict")
    expected = options.expected_manifest_sha256
    if (
        expected is None
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        raise VoiceLabError("model eviction requires a lowercase --expected-manifest-sha256")
    orchestrator_lock = layout.directory("runtimes") / "audition-orchestrator.lock"
    worker_lock = layout.directory("runtimes") / "worker.lock"
    runtime_root = layout.directory("runtimes")
    with (
        exclusive_worker_slot(
            orchestrator_lock,
            boundary=runtime_root,
        ),
        exclusive_worker_slot(worker_lock, boundary=runtime_root),
    ):
        removed_bytes = execute_source_eviction(
            source,
            plan,
            boundary=boundary,
            expected_manifest_sha256=expected,
            confirmed=True,
        )
    result.update({"removed": True, "removed_bytes": removed_bytes})
    _json_print(result)
    return 0


def _models_evict_runtime(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    registry = _registry(options)
    runtime = registry.runtime(options.runtime_id)
    boundary = layout.directory("runtimes")
    plan = build_runtime_eviction_plan(
        runtime,
        boundary / runtime.runtime_id,
        boundary=boundary,
        repository_root=_repository_root(),
    )
    result: dict[str, object] = {
        "runtime_id": plan.runtime_id,
        "path": str(plan.path),
        "receipt_sha256": plan.receipt_sha256,
        "estimated_bytes": plan.estimated_bytes,
        "execution_requested": options.execute,
        "network_used": False,
        "irreversible": True,
    }
    if not options.execute:
        result["removed"] = False
        _json_print(result)
        return 0
    if not options.confirm_evict:
        raise VoiceLabError("runtime eviction requires --confirm-evict")
    expected = options.expected_receipt_sha256
    if (
        expected is None
        or len(expected) != 64
        or any(character not in "0123456789abcdef" for character in expected)
    ):
        raise VoiceLabError("runtime eviction requires a lowercase --expected-receipt-sha256")
    removed_bytes = execute_runtime_eviction(
        runtime,
        plan,
        boundary=boundary,
        repository_root=_repository_root(),
        expected_receipt_sha256=expected,
        orchestration_lock_path=boundary / "audition-orchestrator.lock",
        confirmed=True,
    )
    result.update({"removed": True, "removed_bytes": removed_bytes})
    _json_print(result)
    return 0


def _audition_plan(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    registry = _registry(options)
    registry.verify_requirements(_repository_root())
    suite = load_prompt_suite(options.suite)
    if suite.purpose != "held-out-evaluation":
        raise VoiceLabError("audition planning requires a held-out evaluation suite")
    prompts = _selected_prompts(suite, options.prompt_id)
    references = _audition_references(layout, options.reference_id)
    sources = _audition_sources(
        layout,
        options.source_audio,
        {prompt.prompt_id for prompt in prompts},
    )
    plan = build_audition_plan(
        registry,
        layout,
        experiment_id=options.experiment_id,
        prompt_suite=suite,
        prompts=prompts,
        references=references,
        seeds=tuple(options.seed),
        source_audio_by_prompt=sources,
        profile=options.profile,
        direct_timing_modes=tuple(options.timing_mode or ("cold", "warm")),
    )
    path, digest = persist_audition_plan(layout, plan)
    counts: dict[str, int] = {}
    for trial in plan.trials:
        counts[trial.candidate_id] = counts.get(trial.candidate_id, 0) + 1
    _json_print(
        {
            "plan_path": str(path),
            "plan_sha256": digest,
            "experiment_id": plan.experiment_id,
            "profile": plan.profile,
            "trial_count": len(plan.trials),
            "trials_by_candidate": dict(sorted(counts.items())),
            "network_used": False,
            "execution_performed": False,
        }
    )
    return 0


def _audition_run(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    registry = _registry(options)
    registry.verify_requirements(_repository_root())
    plan = _load_audition_plan(layout, options.experiment_id)
    candidate_ids = options.candidate
    if len(candidate_ids) != len(set(candidate_ids)):
        raise VoiceLabError("audition candidate selection must be unique")
    direct: list[str] = []
    voice_conversion: list[str] = []
    for candidate_id in candidate_ids:
        candidate = registry.candidate(candidate_id)
        if not any(trial.candidate_id == candidate_id for trial in plan.trials):
            raise VoiceLabError(f"candidate is not present in the audition plan: {candidate_id}")
        if candidate.track == "direct-tts":
            direct.append(candidate_id)
        else:
            voice_conversion.append(candidate_id)
    selected_trials = [
        trial
        for trial in plan.trials
        if trial.candidate_id in set(candidate_ids)
        and (not options.trial_id or trial.trial_id in set(options.trial_id))
    ]
    if not selected_trials:
        raise VoiceLabError("audition selection contains no trials")
    runtime_receipts: dict[str, str] = {}
    for binding in options.runtime_receipt:
        runtime_id, separator, digest = binding.partition("=")
        if (
            not separator
            or not runtime_id
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or runtime_id in runtime_receipts
        ):
            raise VoiceLabError(
                "--runtime-receipt requires unique RUNTIME_ID=lowercase-SHA256 bindings"
            )
        runtime_receipts[runtime_id] = digest
    required_runtime_ids = {trial.runtime_id for trial in selected_trials}
    if options.execute and set(runtime_receipts) != required_runtime_ids:
        raise VoiceLabError(
            "audition execution requires exactly one approved --runtime-receipt for every "
            "selected runtime"
        )
    if not options.execute:
        _json_print(
            {
                "experiment_id": plan.experiment_id,
                "candidate_ids": candidate_ids,
                "trial_ids": [trial.trial_id for trial in selected_trials],
                "execution_requested": False,
                "network_allowed": False,
                "message": "rerun with --execute after prepared-runtime review",
            }
        )
        return 0
    manifest = run_offline_audition(
        registry,
        layout,
        plan,
        direct_candidate_ids=direct,
        voice_conversion_candidate_ids=voice_conversion,
        trial_ids=tuple(options.trial_id) if options.trial_id else None,
        repository_root=_repository_root(),
        expected_runtime_receipt_sha256=runtime_receipts,
    )
    _json_print(
        {
            "experiment_id": manifest.experiment_id,
            "status": manifest.status,
            "result_count": len(manifest.results),
            "run_manifest": str(audition_paths(layout, plan.experiment_id).run_manifest_path),
            "network_used": False,
        }
    )
    return 0


def _audition_status(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    registry = _registry(options)
    registry.verify_requirements(_repository_root())
    plan = _load_audition_plan(layout, options.experiment_id)
    paths = audition_paths(layout, plan.experiment_id)
    if not paths.run_manifest_path.exists():
        _json_print(
            {
                "experiment_id": plan.experiment_id,
                "status": "not-started",
                "trial_count": len(plan.trials),
            }
        )
        return 0
    manifest = load_validated_audition_run(
        registry,
        layout,
        plan,
        repository_root=_repository_root(),
    )
    _json_print(manifest.model_dump(mode="json"))
    return 0


def _blind_build(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    tracks = tuple(options.track or ("direct-tts",))
    inputs = blind_inputs_from_audition(
        layout,
        experiment_id=options.experiment_id,
        tracks=tracks,
    )
    package = build_review_package(
        layout,
        review_id=options.review_id,
        inputs=inputs,
    )
    _json_print(
        {
            "review_id": package.index.review_id,
            "package_root": str(package.root),
            "public_root": str(package.root / "public"),
            "sample_count": len(package.index.samples),
            "review_index_sha256": package.review_index_sha256,
            "public_manifest_sha256": package.public_manifest_sha256,
            "package_manifest_sha256": package.package_manifest_sha256,
            "candidate_labels_exposed": False,
        }
    )
    return 0


def _blind_verify(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    package = verify_review_package(layout, review_id=options.review_id)
    _json_print(
        {
            "review_id": package.index.review_id,
            "sample_count": len(package.index.samples),
            "review_index_sha256": package.review_index_sha256,
            "public_manifest_sha256": package.public_manifest_sha256,
            "package_manifest_sha256": package.package_manifest_sha256,
            "verified": True,
        }
    )
    return 0


def _blind_aggregate(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    sheet = _read_bounded_contract(Path(options.score_sheet), ScoreSheet)
    _json_print(aggregate_scores(layout, sheet))
    return 0


def _promotion_validate(options: argparse.Namespace) -> int:
    manifest = _read_bounded_contract(Path(options.manifest), PromotionManifest)
    validation = validate_promotion(
        manifest,
        asset_root=_asset_root(options.asset_root),
        as_of=options.as_of,
    )
    _json_print({"status": "pass", **validation.model_dump(mode="json")})
    return 0


def _promotion_handoff(options: argparse.Namespace) -> int:
    requested_output = _new_output_path(Path(options.output))
    manifest = _read_bounded_contract(Path(options.manifest), PromotionManifest)
    handoff = build_chloespeech_handoff(
        manifest,
        asset_root=_asset_root(options.asset_root),
        as_of=options.as_of,
    )
    output, output_sha256 = _write_new_canonical_json(requested_output, handoff)
    _json_print(
        {
            "output_path": str(output),
            "output_sha256": output_sha256,
            "promotion_sha256": handoff.promotion_sha256,
        }
    )
    return 0


def _consent_install(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    path, digest = install_pending_consent(layout)
    with open_regular_no_follow(path) as stream:
        record = ConsentRecord.model_validate_json(stream.read())
    _json_print({"path": str(path), "sha256": digest, "approved": record.approved})
    return 0


def _consent_scope(options: argparse.Namespace) -> dict[str, object]:
    purposes = tuple(sorted(options.purpose))
    boundaries = tuple(sorted(options.execution_boundary))
    if len(purposes) != len(set(purposes)) or len(boundaries) != len(set(boundaries)):
        raise VoiceLabError("consent purposes and execution boundaries must be unique")
    return {
        "consent_id": options.consent_id,
        "subject_id": options.subject_id,
        "allowed_purposes": purposes,
        "allowed_execution_boundaries": boundaries,
        "expires_at": options.expires_at.isoformat(),
        "hosted_transfer_allowed": False,
        "disclosure_required": True,
    }


def _consent_preview(options: argparse.Namespace) -> int:
    scope = _consent_scope(options)
    _json_print({"scope": scope, "scope_sha256": canonical_sha256(scope), "approved": False})
    return 0


def _consent_approve(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    scope = _consent_scope(options)
    scope_sha256 = canonical_sha256(scope)
    if options.reviewed_scope_sha256 != scope_sha256:
        raise VoiceLabError(
            "approval requires the exact --reviewed-scope-sha256 emitted by consent preview"
        )
    path, digest = approve_self_consent(
        layout,
        acknowledgement=options.i_am_the_subject_and_authorize,
        purposes=tuple(sorted(options.purpose)),
        execution_boundaries=tuple(sorted(options.execution_boundary)),
        expires_at=options.expires_at,
        subject_id=options.subject_id,
        consent_id=options.consent_id,
        expected_current_sha256=options.expected_current_sha256,
    )
    _json_print(
        {
            "path": str(path),
            "sha256": digest,
            "approved": True,
            "scope": scope,
            "scope_sha256": scope_sha256,
        }
    )
    return 0


def _consent_status(options: argparse.Namespace) -> int:
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    path = consent_record_path(layout, options.consent_id)
    digest, size = sha256_file(path)
    with open_regular_no_follow(path) as stream:
        record = ConsentRecord.model_validate_json(stream.read())
    _json_print(
        {
            "path": str(path),
            "sha256": digest,
            "size_bytes": size,
            "record": record.model_dump(mode="json"),
        }
    )
    return 0


def _consent_revoke(options: argparse.Namespace) -> int:
    if not options.confirm_revoke:
        raise VoiceLabError("revocation requires --confirm-revoke")
    layout = AssetLayout.validate(_asset_root(options.asset_root))
    path = consent_record_path(layout, options.consent_id)
    record, digest = revoke_consent(path, expected_sha256=options.expected_sha256)
    _json_print(
        {"path": str(path), "sha256": digest, "revoked_at": record.revoked_at, "approved": False}
    )
    return 0


# -- vault ---------------------------------------------------------------------------------


def _vault_layout(options: argparse.Namespace) -> AssetLayout:
    return AssetLayout.validate(_asset_root(options.asset_root))


def _stdout_is_a_terminal() -> bool:
    """Whether standard output is a terminal; the seam a test replaces."""
    return sys.stdout.isatty()


def _read_standard_input(label: str) -> str:
    """One secret from standard input, bounded and never echoed.

    Raises: ``VoiceLabError`` when nothing arrived, more than
    ``MAX_CLI_SECRET_INPUT_BYTES`` did, or the bytes are not UTF-8; no message repeats them.
    """
    data = sys.stdin.buffer.read(MAX_CLI_SECRET_INPUT_BYTES + 1)
    if len(data) > MAX_CLI_SECRET_INPUT_BYTES:
        raise VoiceLabError(f"{label} exceeds {MAX_CLI_SECRET_INPUT_BYTES} bytes")
    if not data.strip():
        raise VoiceLabError(f"{label} is read from standard input; nothing arrived")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise VoiceLabError(f"{label} must arrive as UTF-8 text") from error


def _vault_usage(message: str) -> VaultCommandError:
    return VaultCommandError("VAULT_CLI.USAGE_INVALID", message)


def _open_bound_vault(layout: AssetLayout, destination: str | None = None) -> LibraryVault:
    """One destination's vault; a destination with no binding is a refusal, not an empty result.

    ``destination`` defaults to the one destination of a single-vault host (the Mac's).
    """
    from .storage_binding import open_library_vault
    from .storage_contracts import DEFAULT_VAULT_DESTINATION

    chosen = DEFAULT_VAULT_DESTINATION if destination is None else destination
    vault = open_library_vault(layout, chosen)
    if vault is None:
        raise storage_error(
            "STORAGE.NOT_BOUND",
            f"destination {chosen} of this library is not bound to a vault; bind one with "
            f"`voice-lab vault bind --destination {chosen}`",
        )
    return vault


def _destination_argument(value: str) -> str:
    """A ``--destination`` value: a short stable id (``laptop``, ``air``)."""
    from .storage_contracts import validate_destination_name

    try:
        return validate_destination_name(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _vault_init(options: argparse.Namespace) -> int:
    from .storage_binding import initialize_library

    layout = _vault_layout(options)
    path, identity = initialize_library(layout, library_id=options.library_id)
    _json_print(
        {
            "path": str(path),
            "library_id": identity.library_id,
            "created_at": identity.created_at.isoformat(),
            "adopted": options.library_id is not None,
        }
    )
    return 0


def _vault_approval(options: argparse.Namespace, library_id: str) -> VaultDestinationApproval:
    from .storage_contracts import VaultDestinationApproval

    return VaultDestinationApproval(
        approval_id=options.approval_id,
        approved=True,
        library_id=library_id,
        destination=options.destination,
        endpoint=options.endpoint,
        tailnet_suffix=options.tailnet_suffix,
        node_stable_id=options.node_stable_id,
        namespace=options.namespace,
        classes=tuple(sorted(set(options.classes))),
        approved_at=options.approved_at or datetime.now().astimezone(),
        expires_at=options.expires_at,
        approved_by=options.approved_by,
        statement=options.statement,
    )


def _approval_file_digest(approval: VaultDestinationApproval) -> str:
    """The digest of the file the approval would be written as (its bytes plus a newline)."""
    return hashlib.sha256(canonical_json_bytes(approval) + b"\n").hexdigest()


def _vault_approve(options: argparse.Namespace) -> int:
    from .storage_binding import (
        read_destination_approval,
        require_library_identity,
        write_destination_approval,
    )

    layout = _vault_layout(options)
    identity = require_library_identity(layout)
    approval = _vault_approval(options, identity.library_id)
    digest = _approval_file_digest(approval)
    if options.preview:
        if options.reviewed_approval_sha256 or options.i_approve_this_destination:
            raise _vault_usage("--preview writes nothing, so it takes no approval flag")
        _json_print(
            {
                "approval": approval.model_dump(mode="json"),
                "approval_sha256": digest,
                "approved": False,
            }
        )
        return 0
    if not options.i_approve_this_destination:
        raise _vault_usage(
            "approving a destination requires --i-approve-this-destination and the "
            "--reviewed-approval-sha256 that `vault approve --preview` printed"
        )
    if options.reviewed_approval_sha256 != digest:
        raise _vault_usage(
            "approval requires the exact --reviewed-approval-sha256 emitted by `vault approve "
            "--preview`; pass the same fields, including the --approved-at it printed"
        )
    path, _ = write_destination_approval(layout, approval)
    written, written_sha256 = read_destination_approval(
        layout, approval.approval_id, approval.destination
    )
    _json_print(
        {
            "path": str(path),
            "sha256": written_sha256,
            "approved": True,
            "approval": written.model_dump(mode="json"),
        }
    )
    return 0


def _vault_bind(options: argparse.Namespace) -> int:
    from .storage_binding import bind_library

    layout = _vault_layout(options)
    result = bind_library(
        layout,
        backend=options.backend,
        endpoint=options.endpoint,
        namespace=options.namespace,
        classes=tuple(options.classes),
        approval_id=options.approval_id,
        approval_sha256=options.approval_sha256,
        replace=options.replace,
        reset_state=options.reset_state,
        destination=options.destination,
    )
    _json_print(
        {
            "path": str(result.path),
            "binding": result.binding.model_dump(mode="json"),
            "replaced": result.replaced,
            "superseded_state": _optional_path(result.superseded_state),
            "superseded_manifests": _optional_path(result.superseded_manifests),
            "takes_effect": "at the next push: the next launch of the recording app (the Mac), "
            "the next run of voice-lab-vault@<destination> (the PC)",
        }
    )
    return 0


def _optional_path(path: Path | None) -> str | None:
    return None if path is None else str(path)


def _vault_approval_summary(
    layout: AssetLayout, binding: StorageBinding | None, now: datetime
) -> JsonValue:
    """What ``vault show`` says about the binding's approval, problems included."""
    from .storage_binding import read_destination_approval

    if binding is None or binding.approval_id is None:
        return None
    try:
        approval, digest = read_destination_approval(
            layout, binding.approval_id, binding.destination
        )
    except StorageError as error:
        return {"approval_id": binding.approval_id, "code": error.code, "problem": str(error)}
    return {
        "approval_id": approval.approval_id,
        "sha256": digest,
        "matches_binding": digest == binding.approval_sha256,
        "effective": approval.effective_at(now),
        "approved_at": approval.approved_at.isoformat(),
        "expires_at": approval.expires_at.isoformat(),
        "node_stable_id": approval.node_stable_id,
        "tag": approval.tag,
        "tailnet_suffix": approval.tailnet_suffix,
        "classes": list(approval.classes),
    }


def _vault_secret_summary(layout: AssetLayout, present: bool, destination: str) -> JsonValue:
    """The key fingerprint and which of the destination's tokens exist; never a secret."""
    from .storage_binding import library_key_fingerprint, vault_token_present

    if not present:
        return {"key_fingerprint": None, "write_token": False, "read_token": False}
    try:
        return {
            "key_fingerprint": library_key_fingerprint(layout),
            "write_token": vault_token_present(layout, "write", destination=destination),
            "read_token": vault_token_present(layout, "read", destination=destination),
        }
    except StorageError as error:
        return {"code": error.code, "problem": str(error)}


def _vault_state(layout: AssetLayout, library_id: str | None, destination: str) -> JsonValue:
    from .library_generations import VaultRecords

    if library_id is None:
        return None
    state = VaultRecords(layout, destination).read_state(library_id)
    return None if state is None else cast(JsonValue, state.model_dump(mode="json"))


def _vault_show(options: argparse.Namespace) -> int:
    from .storage_binding import read_library_identity, read_storage_binding, vault_destinations

    layout = _vault_layout(options)
    destination = options.destination
    identity = read_library_identity(layout)
    binding = read_storage_binding(layout, destination)
    now = datetime.now(UTC)
    library_id = None if identity is None else identity.library_id
    document: dict[str, JsonValue] = {
        "library_id": library_id,
        "destination": destination,
        "destinations": list(vault_destinations(layout)),
        "binding": None if binding is None else binding.model_dump(mode="json"),
        "approval": _vault_approval_summary(layout, binding, now),
        "secrets": _vault_secret_summary(layout, identity is not None, destination),
        "state": _vault_state(layout, library_id, destination),
    }
    if options.json:
        _json_print(document)
        return 0
    for line in _vault_show_lines(document):
        print(line)
    return 0


def _vault_show_lines(document: Mapping[str, JsonValue]) -> list[str]:
    binding = document["binding"]
    lines = [
        f"library: {document['library_id'] or 'not initialized'}",
        f"destination: {document['destination']}",
    ]
    if not isinstance(binding, Mapping):
        lines.append("binding: none (Vault · off)")
    else:
        lines.append(f"binding: {binding['backend']} {binding['endpoint']}")
        lines.append(f"namespace: {binding['namespace']}")
        lines.append(f"classes: {', '.join(cast(list[str], binding['classes']))}")
    for name in ("approval", "secrets", "state"):
        value = document[name]
        lines.append(f"{name}: {'none' if value is None else json.dumps(value, sort_keys=True)}")
    return lines


def _vault_status_line(
    state: str,
    message: str | None,
    *,
    waiting_files: int | None = None,
    unreachable_since: datetime | None = None,
) -> str:
    """The app's vault wording, with local 24-hour time when an outage start is known."""
    if state == "waiting" and waiting_files:
        noun = "file" if waiting_files == 1 else "files"
        return f"Vault · {waiting_files} {noun} waiting"
    if state == "unreachable" and unreachable_since is not None:
        return f"Vault unreachable since {unreachable_since.astimezone():%H:%M}"
    texts = {
        "off": "Vault · off",
        "up-to-date": "Vault · up to date",
        "waiting": "Vault · waiting to copy",
        "copying": "Vault · copying",
        "unreachable": "Vault unreachable",
    }
    return texts.get(state, f"Vault · {message or 'copying stopped'}")


def _vault_status(options: argparse.Namespace) -> int:
    from .library_generations import VaultRecords
    from .storage_binding import open_library_vault
    from .storage_ports import library_head_ref

    layout = _vault_layout(options)
    state = "off"
    code: str | None = None
    message: str | None = None
    generation = 0
    try:
        vault = open_library_vault(layout, options.destination)
        if vault is not None:
            recorded = VaultRecords(layout, options.destination).read_state(vault.library_id)
            generation = 0 if recorded is None else recorded.generation
            head = vault.ref_store.read(library_head_ref(vault.library_id))
            state = (
                "up-to-date"
                if recorded is not None
                and head is not None
                and recorded.head is not None
                and (head.version, head.target) == (recorded.head.version, recorded.head.target)
                else "waiting"
            )
    except StorageError as error:
        state = "unreachable" if error.retryable else "failed"
        code, message = error.code, str(error)
    document: dict[str, JsonValue] = {
        "state": state,
        "line": _vault_status_line(state, message),
        "generation": generation,
        # A file count needs a replication pass; `vault push --dry-run` reports it.
        "waiting_files": None,
        "code": code,
        "message": message,
    }
    if options.json:
        _json_print(document)
    else:
        print(document["line"])
    return 0


def _vault_push(options: argparse.Namespace) -> int:
    from .library_generations import push_generation

    layout = _vault_layout(options)
    vault = _open_bound_vault(layout, options.destination)
    result = push_generation(
        layout,
        blob_store=vault.blob_store,
        ref_store=vault.ref_store,
        library_id=vault.library_id,
        destination=vault.destination,
        classes=vault.classes,
        chunk_bytes=vault.chunk_bytes,
        full=options.full,
        dry_run=options.dry_run,
    )
    _json_print(
        {
            "destination": options.destination,
            "outcome": result.outcome,
            "library_id": result.library_id,
            "generation": result.generation,
            "manifest_sha256": result.manifest_sha256,
            "head_version": None if result.head is None else result.head.version,
            "adopted_generation": result.adopted_generation,
            "uploaded_files": result.uploaded_files,
            "reused_files": result.reused_files,
            "sent_blobs": result.sent_blobs,
            "sent_bytes": result.sent_bytes,
            "waiting_paths": list(result.waiting_paths),
            "owners": [
                {
                    "owner": owner.owner,
                    "state": owner.state,
                    "carried": owner.carried,
                    "reason": owner.reason,
                    "problem_path": owner.problem_path,
                    "held_back_by": owner.held_back_by,
                }
                for owner in result.waiting_owners
            ],
            "source_change": None if result.source_change is None else str(result.source_change),
        }
    )
    return result.exit_status


def _vault_verify(options: argparse.Namespace) -> int:
    from .library_generations import verify_generation

    layout = _vault_layout(options)
    vault = _open_bound_vault(layout, options.destination)
    report = verify_generation(
        layout,
        blob_store=vault.blob_store,
        ref_store=vault.ref_store,
        library_id=vault.library_id,
        generation=options.generation,
        paths=tuple(options.paths) if options.paths else None,
        classes=tuple(options.classes) if options.classes else None,
        destination=options.destination,
    )
    _json_print(
        {
            "destination": options.destination,
            "library_id": report.library_id,
            "generation": report.generation,
            "manifest_sha256": report.manifest_sha256,
            "created_at": report.created_at.isoformat(),
            "checked": len(report.entries),
            "present": report.count("present"),
            "pinned": report.count("pinned"),
            "missing": report.count("missing"),
            "mismatch": report.count("mismatch"),
            "unknown_paths": list(report.unknown_paths),
            "problems": [
                {
                    "path": entry.path,
                    "file_class": entry.file_class,
                    "state": entry.state,
                    "detail": entry.detail,
                }
                for entry in report.entries
                if entry.state in ("missing", "mismatch")
            ],
            "ok": report.ok,
        }
    )
    return report.exit_status


def _vault_restore(options: argparse.Namespace) -> int:
    from .library_generations import VaultRecords, restore_library
    from .storage_binding import open_vault_for_restore

    layout = _vault_layout(options)
    if (options.generation is None) != (options.manifest_sha256 is None):
        raise _vault_usage(
            "restoring a named generation needs both --generation and --manifest-sha256"
        )
    named_vault = options.endpoint is not None and options.backend == "tailnet-vault"
    if named_vault and options.namespace is None:
        raise _vault_usage(
            "restoring from a tailnet vault this Mac is not bound to needs --namespace as well: "
            "it names the vault's namespace for this library"
        )
    if options.endpoint is None:
        vault = _open_bound_vault(layout, options.destination)
        stores = vault.stores
        records = VaultRecords(layout, options.destination)
    else:
        stores = open_vault_for_restore(
            layout,
            backend=options.backend,
            endpoint=options.endpoint,
            namespace=options.namespace,
            node_stable_id=options.node_stable_id,
            tailnet_suffix=options.tailnet_suffix,
            destination=options.destination,
        )
        records = None
    target = Path(os.path.abspath(Path(options.target).expanduser()))
    result = restore_library(
        target,
        blob_store=stores.blob_store,
        ref_store=stores.ref_store,
        library_id=stores.library_id,
        generation=options.generation,
        manifest_sha256=options.manifest_sha256,
        accept_latest=options.accept_latest,
        records=records,
        runtime_root=None if options.runtime_root is None else Path(options.runtime_root),
    )
    _json_print(
        {
            "target": str(result.target),
            "library_id": result.library_id,
            "generation": result.generation,
            "manifest_sha256": result.manifest_sha256,
            "created_at": result.created_at.isoformat(),
            "class_counts": dict(sorted(result.class_counts.items())),
            "restored_files": result.restored_files,
            "restored_bytes": result.restored_bytes,
            "pins": [
                {"path": pin.path, "state": pin.state, "reason": pin.reason} for pin in result.pins
            ],
        }
    )
    # A pin is only expected to land when a runtime was named to re-link it from.
    return 1 if options.runtime_root is not None and result.pin_missing else 0


def _vault_drill(options: argparse.Namespace) -> int:
    """Restore a generation into an empty target, then read every file back and re-hash it.

    The restore drill of design §4.3 and RUNBOOK §9 as one command: ``vault restore`` through
    the bound destination (the head held against this host's records), then
    ``rehash_restored_library`` over what landed on disk. It never removes the target.
    """
    from .library_generations import VaultRecords, rehash_restored_library, restore_library

    layout = _vault_layout(options)
    if (options.generation is None) != (options.manifest_sha256 is None):
        raise _vault_usage(
            "a drill of a named generation needs both --generation and --manifest-sha256"
        )
    vault = _open_bound_vault(layout, options.destination)
    target = Path(os.path.abspath(Path(options.target).expanduser()))
    result = restore_library(
        target,
        blob_store=vault.blob_store,
        ref_store=vault.ref_store,
        library_id=vault.library_id,
        generation=options.generation,
        manifest_sha256=options.manifest_sha256,
        accept_latest=options.accept_latest,
        records=VaultRecords(layout, options.destination),
    )
    report = rehash_restored_library(result.target, result.manifest)
    _json_print(
        {
            "destination": options.destination,
            "target": str(result.target),
            "library_id": result.library_id,
            "generation": result.generation,
            "manifest_sha256": result.manifest_sha256,
            "created_at": result.created_at.isoformat(),
            "class_counts": dict(sorted(result.class_counts.items())),
            "restored_files": result.restored_files,
            "restored_bytes": result.restored_bytes,
            "rehashed_files": len(report.entries),
            "rehashed_bytes": report.checked_bytes,
            "pins_not_restored": len(result.pins),
            "problems": [
                {"path": entry.path, "state": entry.state, "detail": entry.detail}
                for entry in report.problems
            ],
            "ok": report.ok,
        }
    )
    return report.exit_status


def _vault_audit(options: argparse.Namespace) -> int:
    from .storage_binding import (
        audit_vault_access,
        read_access_records,
        read_request_records,
        read_storage_binding,
    )

    layout = _vault_layout(options)
    binding = read_storage_binding(layout, options.destination)
    namespace = options.namespace or (None if binding is None else binding.namespace)
    if namespace is None:
        raise _vault_usage("auditing needs a namespace: bind the vault first, or pass --namespace")
    report = audit_vault_access(
        read_access_records(Path(options.access_log).expanduser()),
        read_request_records(layout, options.destination),
        namespace,
    )
    _json_print(
        {
            "namespace": report.namespace,
            "server_accesses": report.server_accesses,
            "mac_requests": report.mac_requests,
            "matched": report.matched,
            "unanswered": report.unanswered,
            "refused_at_server": report.refused_at_server,
            "unexplained": [
                {
                    "time": access.time.isoformat(),
                    "operation": access.operation,
                    "ref": access.ref,
                    "status": access.status,
                    "bytes": access.bytes,
                    "scope": access.scope,
                    "user_login": access.user_login,
                }
                for access in report.unexplained
            ],
            "ok": not report.unexplained,
        }
    )
    return report.exit_status


def _vault_key_create(options: argparse.Namespace) -> int:
    from .storage_binding import (
        check_recovery_sheet_target,
        create_library_vault_key,
        write_library_recovery_sheet,
    )

    layout = _vault_layout(options)
    if bool(options.sheet_out) == bool(options.print_to_terminal):
        raise _vault_usage(
            "creating a library key needs exactly one of --sheet-out PATH (a new private file "
            "outside the library) or --print-to-terminal"
        )
    sheet_path: Path | None = None
    if options.sheet_out:
        sheet_path = check_recovery_sheet_target(layout, _new_output_path(Path(options.sheet_out)))
    elif not _stdout_is_a_terminal():
        raise VaultCommandError(
            "VAULT_CLI.NOT_A_TERMINAL",
            "--print-to-terminal prints the recovery sheet, which holds the library key, so it "
            "is refused unless standard output is a terminal; use --sheet-out PATH instead",
        )
    created = create_library_vault_key(layout)
    if sheet_path is None:
        # The only output: the sheet itself, on a terminal nothing is capturing.
        print(created.sheet)
        return 0
    try:
        written = write_library_recovery_sheet(layout, sheet_path, created.sheet)
    except StorageError as error:
        # The key exists now and the sheet may not: say so, because `vault key create` will
        # refuse to make a second key and no command renders a sheet from the Keychain.
        raise storage_error(error.code, _sheet_not_written(created, sheet_path, error)) from error
    _json_print(
        {
            "library_id": created.library_id,
            "key_fingerprint": created.fingerprint,
            "recovery_sheet": str(written),
        }
    )
    return 0


def _sheet_not_written(created: LibraryKeyCreation, path: Path, error: StorageError) -> str:
    """What to do when the key is in the keychain but its sheet may not be on disk."""
    from .storage_contracts import library_key_item

    item = library_key_item(created.library_id)
    return (
        f"{error}; library {created.library_id}'s key {created.fingerprint} now exists as item "
        f"{item} and nothing has been copied to a vault yet, so unless {path} holds a complete "
        "recovery sheet, delete that item (on macOS `security delete-generic-password -s "
        f"{item}`, on Linux its file in the credential staging directory) and run `voice-lab "
        "vault key create` again against a writable path, so the key and its offline sheet are "
        "made together"
    )


def _vault_key_import(options: argparse.Namespace) -> int:
    from .storage_binding import import_library_vault_key

    layout = _vault_layout(options)
    if options.sheet == "-":
        text = _read_standard_input("a recovery sheet")
    else:
        text = _read_recovery_sheet_file(Path(options.sheet).expanduser())
    fingerprint = import_library_vault_key(layout, text)
    _json_print({"library_id": _library_id_of(layout), "key_fingerprint": fingerprint})
    return 0


def _read_recovery_sheet_file(path: Path) -> str:
    with open_regular_no_follow(path, nonblocking=True) as stream:
        data = stream.read(MAX_CLI_SECRET_INPUT_BYTES + 1)
    if len(data) > MAX_CLI_SECRET_INPUT_BYTES:
        raise VoiceLabError(f"a recovery sheet exceeds {MAX_CLI_SECRET_INPUT_BYTES} bytes: {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise VoiceLabError(f"a recovery sheet must be UTF-8 text: {path}") from error


def _library_id_of(layout: AssetLayout) -> str:
    from .storage_binding import require_library_identity

    return require_library_identity(layout).library_id


def _vault_key_show(options: argparse.Namespace) -> int:
    from .storage_binding import library_key_fingerprint

    layout = _vault_layout(options)
    _json_print(
        {"library_id": _library_id_of(layout), "key_fingerprint": library_key_fingerprint(layout)}
    )
    return 0


def _vault_namespace(layout: AssetLayout, destination: str, named: str | None) -> str:
    from .storage_binding import read_storage_binding

    binding = read_storage_binding(layout, destination)
    namespace = named or (None if binding is None else binding.namespace)
    if namespace is None:
        raise _vault_usage(
            "the tokens belong to one vault namespace: bind the vault first, or pass --namespace"
        )
    return namespace


def _vault_token_create(options: argparse.Namespace) -> int:
    from .storage_binding import create_vault_tokens
    from .storage_contracts import keychain_item_names

    layout = _vault_layout(options)
    destination = options.destination
    tokens = create_vault_tokens(
        layout, _vault_namespace(layout, destination, options.namespace), destination=destination
    )
    names = keychain_item_names(tokens.library_id, destination)
    _json_print(
        {
            "library_id": tokens.library_id,
            "destination": tokens.destination,
            "namespace": tokens.namespace,
            "write_token_sha256": tokens.write_token_sha256,
            "read_token_sha256": tokens.read_token_sha256,
            "write_token_item": names.token,
            "read_token_item": names.read_token,
            "tokens_kept_in": "the keychain (login Keychain on macOS, credential directory on "
            "Linux); neither token is printed",
        }
    )
    return 0


def _vault_token_import(options: argparse.Namespace) -> int:
    from .storage_binding import import_vault_token

    layout = _vault_layout(options)
    digest = import_vault_token(
        layout,
        options.scope,
        _read_standard_input(f"a {options.scope} vault token"),
        destination=options.destination,
    )
    _json_print(
        {
            "library_id": _library_id_of(layout),
            "destination": options.destination,
            "scope": options.scope,
            "token_sha256": digest,
        }
    )
    return 0


def _add_consent_scope_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--subject-id", default="cole-self")
    parser.add_argument("--consent-id", default="cole-self-v1")
    parser.add_argument(
        "--purpose",
        action="append",
        required=True,
        choices=(
            "private-zero-shot-evaluation",
            "private-fine-tuning",
            "private-chloe-speech-rendering",
        ),
    )
    parser.add_argument(
        "--execution-boundary",
        action="append",
        required=True,
        choices=("capture-host", "tailnet-local-host"),
    )
    parser.add_argument("--expires-at", type=_aware_datetime, required=True)


def _add_capture_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--asset-root")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--subject-id", default="cole-self")
    parser.add_argument(
        "--suite", default="pilot", choices=("pilot", "evaluation", "pilot-v1", "evaluation-v1")
    )
    parser.add_argument("--device", help="exact AVFoundation device ID or display name")
    parser.add_argument("--microphone-serial")
    parser.add_argument("--ffmpeg", default="/opt/homebrew/bin/ffmpeg")


def _add_registry_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidates", help="candidate TOML; defaults to the pinned repo config")
    parser.add_argument("--model-lock", help="source lock JSON; defaults to the pinned repo lock")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voice-lab",
        description="Private voice capture and reproducible local-model evaluation.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    doctor = commands.add_parser("doctor", help="run read-only host readiness checks")
    doctor.add_argument("--asset-root")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(handler=_doctor)

    desktop = commands.add_parser("desktop", help="open the native recording library window")
    desktop.add_argument("--asset-root")
    desktop.add_argument(
        "--suite", default="pilot", help="bundled suite or explicit prompt-suite JSON"
    )
    desktop.add_argument("--subject-id", default="cole-self")
    desktop.add_argument("--device", help="preferred UMIK-1 ID or display name")
    desktop.add_argument("--ffmpeg", default="/opt/homebrew/bin/ffmpeg")
    desktop.add_argument("--pilot-gate-path", help="reviewed private gate path for a full corpus")
    desktop.add_argument("--pilot-gate-sha256", help="exact reviewed full-corpus gate digest")
    desktop.set_defaults(handler=_desktop)

    assets = commands.add_parser("assets", help="manage the explicit private asset layout")
    asset_commands = assets.add_subparsers(dest="assets_command", required=True)
    assets_init = asset_commands.add_parser("init")
    assets_init.add_argument("--asset-root", required=True)
    assets_init.add_argument(
        "--diagnostic",
        choices=("physical-test",),
        help="create a new marked root for physical-test catalogs only",
    )
    assets_init.add_argument("--subject-id", help="diagnostic subject bound by the root marker")
    assets_init.set_defaults(handler=_assets_init)
    assets_status = asset_commands.add_parser("status")
    assets_status.add_argument("--asset-root")
    assets_status.set_defaults(handler=_assets_status)

    published_help = (
        "a published <catalog_id>[@<version>] (latest version without @), resolved through "
        "the library's corpus store, which takes its lock and opens its database"
    )
    catalog_help = (
        "a released built-in (" + ", ".join(BUILTIN_CATALOG_RESOURCES) + ") or " + published_help
    )
    prompts = commands.add_parser("prompts", help="inspect bundled and published prompt suites")
    prompt_commands = prompts.add_subparsers(dest="prompts_command", required=True)
    prompt_show = prompt_commands.add_parser("show")
    prompt_show.add_argument(
        "suite",
        metavar="catalog",
        help="a built-in name (" + ", ".join(PROMPT_SHOW_RESOURCES) + ") or " + published_help,
    )
    prompt_show.add_argument("--asset-root", help="library holding published catalogs")
    prompt_show.add_argument("--json", action="store_true")
    prompt_show.set_defaults(handler=_prompts_show)
    builtin_suites = tuple(BUILTIN_CATALOG_RESOURCES)
    coverage_report = prompt_commands.add_parser(
        "coverage-report",
        help="exact coverage of a built-in or published catalog, reading evidence without "
        "writing it (a published catalog is resolved through the corpus store first); exit 1 "
        "on unexplained losses",
    )
    coverage_report.add_argument("--asset-root")
    coverage_report.add_argument("--subject-id", required=True)
    coverage_report.add_argument("--catalog", required=True, help=catalog_help)
    coverage_report.add_argument("--baseline", help=catalog_help)
    coverage_report.add_argument("--json", action="store_true")
    coverage_report.set_defaults(handler=_prompts_coverage_report)

    finetune = commands.add_parser(
        "finetune", help="fine-tune a TTS model from the latest kept take of every prompt"
    )
    finetune_commands = finetune.add_subparsers(dest="finetune_command", required=True)

    def _finetune_corpus_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--asset-root")
        parser.add_argument("--subject-id", default="cole-self")
        parser.add_argument("--catalog", required=True, help=catalog_help)
        parser.add_argument(
            "--allow-missing",
            action="store_true",
            help="proceed with the prompts that have a take instead of refusing",
        )

    def _finetune_run_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--output-root", default="derived/finetune")
        parser.add_argument("--run-id", help="defaults to finetune-<UTC time>")
        parser.add_argument("--ffmpeg", default="/opt/homebrew/bin/ffmpeg")

    def _finetune_training_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--runtime-root", required=True, help="provisioned F5-TTS runtime")
        parser.add_argument("--epochs", type=int)
        parser.add_argument("--learning-rate", type=float)
        parser.add_argument("--batch-frames", type=int)
        parser.add_argument("--max-samples-per-batch", type=int)
        parser.add_argument("--warmup-updates", type=int)
        parser.add_argument("--save-per-updates", type=int)
        parser.add_argument("--keep-last-n-checkpoints", type=int)
        parser.add_argument("--last-per-updates", type=int)
        parser.add_argument("--dataloader-workers", type=int)
        parser.add_argument("--no-light-checkpoints", action="store_true")

    def _finetune_sample_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--reference-prompt-id", help="defaults to the first corpus clip")
        parser.add_argument("--text", action="append", help="sentence to synthesize; repeatable")
        parser.add_argument("--no-ema", action="store_true", help="use raw instead of EMA weights")
        parser.add_argument("--nfe-steps", type=int, default=32)
        parser.add_argument("--device", choices=("mps", "cpu"), default="mps")

    finetune_corpus = finetune_commands.add_parser(
        "corpus", help="list the latest kept take per spoken prompt (read-only)"
    )
    _finetune_corpus_arguments(finetune_corpus)
    finetune_corpus.add_argument("--json", action="store_true")
    finetune_corpus.set_defaults(handler=_finetune_corpus)

    finetune_dataset = finetune_commands.add_parser(
        "dataset", help="write normalized 24 kHz training copies under derived/finetune/<run>"
    )
    _finetune_corpus_arguments(finetune_dataset)
    _finetune_run_arguments(finetune_dataset)
    finetune_dataset.set_defaults(handler=_finetune_dataset)

    finetune_train = finetune_commands.add_parser(
        "train", help="fine-tune F5-TTS on a materialized run dataset (blocks until done)"
    )
    finetune_train.add_argument("--asset-root")
    finetune_train.add_argument("--output-root", default="derived/finetune")
    finetune_train.add_argument("--run-id", required=True)
    _finetune_training_arguments(finetune_train)
    finetune_train.set_defaults(handler=_finetune_train)

    finetune_synthesize = finetune_commands.add_parser(
        "synthesize", help="generate listening samples from a run's checkpoint or the base"
    )
    finetune_synthesize.add_argument("--asset-root")
    finetune_synthesize.add_argument("--output-root", default="derived/finetune")
    finetune_synthesize.add_argument("--run-id", required=True)
    finetune_synthesize.add_argument("--runtime-root", required=True)
    finetune_synthesize.add_argument(
        "--checkpoint", default="last", help="'last', 'base', or a checkpoint path"
    )
    finetune_synthesize.add_argument("--label", default="finetuned")
    _finetune_sample_arguments(finetune_synthesize)
    finetune_synthesize.set_defaults(handler=_finetune_synthesize)

    finetune_run = finetune_commands.add_parser(
        "run", help="corpus → dataset → train → samples (fine-tuned and base) in one go"
    )
    _finetune_corpus_arguments(finetune_run)
    _finetune_run_arguments(finetune_run)
    _finetune_training_arguments(finetune_run)
    _finetune_sample_arguments(finetune_run)
    finetune_run.set_defaults(handler=_finetune_run)

    finetune_provision = finetune_commands.add_parser(
        "provision-runtime",
        help="create the pinned F5-TTS runtime venv and fetch the pinned weights (plan by default)",
    )
    finetune_provision.add_argument("--runtime-root", required=True)
    finetune_provision.add_argument(
        "--python", help="interpreter for the venv; Homebrew 3.12 by default"
    )
    finetune_provision.add_argument(
        "--requirements", help="pinned requirements; this checkout's by default"
    )
    finetune_provision.add_argument("--execute", action="store_true")
    finetune_provision.add_argument("--allow-downloads", action="store_true")
    finetune_provision.set_defaults(handler=_finetune_provision_runtime)

    finetune_fetch = finetune_commands.add_parser(
        "fetch-pretrained", help="fetch or verify the pinned pretrained weights and vocabulary"
    )
    finetune_fetch.add_argument("--runtime-root", required=True)
    finetune_fetch.add_argument("--allow-downloads", action="store_true")
    finetune_fetch.set_defaults(handler=_finetune_fetch_pretrained)

    finetune_archive = finetune_commands.add_parser(
        "archive",
        help="copy one finished run to the library vault, verify it there and record it as "
        "archived",
    )
    finetune_archive.add_argument("--asset-root")
    finetune_archive.add_argument("--output-root", default="derived/finetune")
    finetune_archive.add_argument("--run", required=True, help="the run id to archive")
    finetune_archive.add_argument(
        "--remove-local-copy",
        action="store_true",
        help="after the copy verifies, remove the run's checkpoints from this Mac",
    )
    finetune_archive.set_defaults(handler=_finetune_archive)

    finetune_restore_run = finetune_commands.add_parser(
        "restore-run", help="bring an archived run's removed files back from the library vault"
    )
    finetune_restore_run.add_argument("--asset-root")
    finetune_restore_run.add_argument("--output-root", default="derived/finetune")
    finetune_restore_run.add_argument("--run", required=True, help="the run id to restore")
    finetune_restore_run.add_argument(
        "--runtime-root",
        help="the fine-tune runtime to re-link the pinned base weights from; without it the "
        "pin is reported missing and the rest of the run is still restored",
    )
    finetune_restore_run.set_defaults(handler=_finetune_restore_run)

    catalog = commands.add_parser(
        "catalog", help="edit, import, publish and activate prompt catalogs (the corpus store)"
    )
    catalog_commands = catalog.add_subparsers(dest="catalog_command", required=True)
    purposes = get_args(PromptSuitePurpose)

    def _catalog_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--asset-root")

    def _catalog_id_argument(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--catalog-id", default=DEFAULT_CATALOG_ID)

    def _prompt_field_arguments(parser: argparse.ArgumentParser, *, adding: bool) -> None:
        text = parser.add_mutually_exclusive_group()
        text.add_argument("--text", help="the sentence read aloud")
        instructions = parser.add_mutually_exclusive_group()
        instructions.add_argument("--instructions", help="what a silent prompt asks for")
        parser.add_argument("--category", help="defaults to neutral" if adding else None)
        parser.add_argument(
            "--style", help="reading guidance; defaults to plain reading" if adding else None
        )
        parser.add_argument(
            "--expected-seconds",
            type=float,
            help="defaults to 0.42 s per word + 0.8 s for spoken text" if adding else None,
        )
        labels = parser.add_mutually_exclusive_group()
        labels.add_argument("--label", action="append", help="repeatable")
        if adding:
            parser.add_argument("--reference-candidate", action="store_true")
            parser.add_argument("--holdout", action="store_true")
            return
        text.add_argument("--no-text", action="store_true", help="make the prompt silent")
        instructions.add_argument("--no-instructions", action="store_true")
        labels.add_argument("--no-labels", action="store_true")
        parser.add_argument("--reference-candidate", action=argparse.BooleanOptionalAction)
        parser.add_argument("--holdout", action=argparse.BooleanOptionalAction)

    catalog_init = catalog_commands.add_parser(
        "init", help="create the store, seed a built-in, publish version 1 and activate it"
    )
    _catalog_arguments(catalog_init)
    catalog_init.add_argument("--subject-id", required=True)
    catalog_init.add_argument("--catalog-id", default=DEFAULT_CATALOG_ID)
    catalog_init.add_argument("--suite-id", help="suite id of every version; the catalog id")
    catalog_init.add_argument("--from-builtin", choices=builtin_suites)
    catalog_init.add_argument("--drop-room-tones", action="store_true")
    catalog_init.add_argument("--purpose", choices=purposes, help="defaults to the seed's purpose")
    catalog_init.add_argument("--description", default="")
    catalog_init.set_defaults(handler=_catalog_init)

    catalog_create = catalog_commands.add_parser("create", help="create an empty catalog")
    _catalog_arguments(catalog_create)
    catalog_create.add_argument("catalog_id")
    catalog_create.add_argument("--purpose", required=True, choices=purposes)
    catalog_create.add_argument("--suite-id", help="suite id of every version; the catalog id")
    catalog_create.add_argument("--description", default="")
    catalog_create.set_defaults(handler=_catalog_create)

    catalog_list = catalog_commands.add_parser(
        "list", help="catalogs, their versions and prompt counts (read-only)"
    )
    _catalog_arguments(catalog_list)
    catalog_list.add_argument("--subject-id", help="also name this subject's active version")
    catalog_list.add_argument("--json", action="store_true")
    catalog_list.set_defaults(handler=_catalog_list)

    catalog_show = catalog_commands.add_parser(
        "show", help="one published version, latest by default (read-only)"
    )
    _catalog_arguments(catalog_show)
    _catalog_id_argument(catalog_show)
    catalog_show.add_argument("--version", type=_positive_number, help="default: latest")
    catalog_show.add_argument("--json", action="store_true")
    catalog_show.set_defaults(handler=_catalog_show)

    catalog_export = catalog_commands.add_parser(
        "export", help="print a published version as prompt-suite JSON (read-only)"
    )
    _catalog_arguments(catalog_export)
    _catalog_id_argument(catalog_export)
    catalog_export.add_argument("--version", type=_positive_number, help="default: latest")
    catalog_export.add_argument("--json", action="store_true", required=True)
    catalog_export.set_defaults(handler=_catalog_export)

    catalog_add = catalog_commands.add_parser("add-prompt", help="add a prompt (revision 1)")
    _catalog_arguments(catalog_add)
    catalog_add.add_argument("prompt_id")
    _prompt_field_arguments(catalog_add, adding=True)
    catalog_add.set_defaults(handler=_catalog_add_prompt)

    catalog_edit = catalog_commands.add_parser(
        "edit-prompt",
        help="append a revision; earlier recordings stop counting unless --keep-recordings",
    )
    _catalog_arguments(catalog_edit)
    catalog_edit.add_argument("prompt_id")
    _prompt_field_arguments(catalog_edit, adding=False)
    catalog_edit.add_argument(
        "--keep-recordings",
        action="store_true",
        help="typo-class fix (at most 2 word and 3 character edits beyond case and "
        "punctuation): earlier recordings keep counting and train with the new text",
    )
    catalog_edit.add_argument(
        "--allow-rewording",
        action="store_true",
        help="with --keep-recordings: keep them for a larger rewording the recordings already say",
    )
    catalog_edit.add_argument(
        "--subject-id", default="cole-self", help="whose recordings are counted"
    )
    catalog_edit.add_argument(
        "--expected-revision",
        type=_positive_number,
        help="refuse unless the prompt is still at this revision (the one a preview showed)",
    )
    catalog_edit.add_argument(
        "--dry-run", action="store_true", help="count and check, but write nothing"
    )
    catalog_edit.add_argument(
        "--json",
        action="store_true",
        help="one JSON document on stdout, after the write; the preview lines go to stderr "
        "first (preview with --dry-run --json, then write with --expected-revision)",
    )
    catalog_edit.set_defaults(handler=_catalog_edit_prompt)

    catalog_retire = catalog_commands.add_parser(
        "retire-prompt", help="leave a prompt out of every later version"
    )
    _catalog_arguments(catalog_retire)
    catalog_retire.add_argument("prompt_id")
    catalog_retire.set_defaults(handler=_catalog_retire_prompt)

    for name, handler in (
        ("import-harvard", _catalog_import_harvard),
        ("import-arctic", _catalog_import_arctic),
    ):
        importer = catalog_commands.add_parser(
            name, help="import a standard sentence set from a saved copy or an explicit fetch"
        )
        _catalog_arguments(importer)
        origin = importer.add_mutually_exclusive_group(required=True)
        origin.add_argument("--from-file", help="a saved copy of the published list")
        origin.add_argument(
            "--fetch", action="store_true", help="download the published list (network)"
        )
        importer.add_argument(
            "--fetched-at",
            type=_aware_datetime,
            help="when the --from-file copy was downloaded (ISO-8601 with offset)",
        )
        importer.add_argument(
            "--source-url", help="where the --from-file copy was downloaded from (http/https)"
        )
        importer.add_argument(
            "--accept-unpinned-source",
            action="store_true",
            help="import a list whose sha256 is not the published list's (recorded in every "
            "prompt's source reference)",
        )
        importer.set_defaults(handler=handler)

    catalog_import_json = catalog_commands.add_parser(
        "import-json", help="import every prompt of a prompt-suite JSON file"
    )
    _catalog_arguments(catalog_import_json)
    catalog_import_json.add_argument("suite_path", metavar="prompt_suite_json")
    catalog_import_json.set_defaults(handler=_catalog_import_json)

    catalog_publish = catalog_commands.add_parser(
        "publish", help="publish every live prompt, in insertion order, as a new version"
    )
    _catalog_arguments(catalog_publish)
    _catalog_id_argument(catalog_publish)
    catalog_publish.set_defaults(handler=_catalog_publish)

    catalog_activate = catalog_commands.add_parser(
        "activate", help="make a published version the subject's (next app launch)"
    )
    _catalog_arguments(catalog_activate)
    catalog_activate.add_argument("--subject-id", required=True)
    _catalog_id_argument(catalog_activate)
    catalog_activate.add_argument("--version", type=_positive_number, required=True)
    catalog_activate.set_defaults(handler=_catalog_activate)

    capture = commands.add_parser("capture", help="foreground UMIK-1 capture on macOS")
    capture_commands = capture.add_subparsers(dest="capture_command", required=True)
    capture_devices = capture_commands.add_parser("devices", help="list AVFoundation inputs")
    capture_devices.add_argument("--ffmpeg", default="/opt/homebrew/bin/ffmpeg")
    capture_devices.set_defaults(handler=_capture_devices)
    capture_run = capture_commands.add_parser("run", help="record and finalize an exact suite")
    _add_capture_arguments(capture_run)
    capture_run.set_defaults(handler=_capture_run)
    capture_console = capture_commands.add_parser(
        "console", help="record, review, and finalize an exact suite in a local console"
    )
    _add_capture_arguments(capture_console)
    capture_console.add_argument("--resume", action="store_true")
    capture_console.add_argument("--open", action="store_true")
    capture_console.set_defaults(handler=_capture_console)

    recording = commands.add_parser("recording", help="native recording metadata capabilities")
    recording_commands = recording.add_subparsers(dest="recording_command", required=True)
    recording_service = recording_commands.add_parser(
        "service", help="serve strict control over inherited local stdin/stdout pipes"
    )
    recording_service.add_argument("--asset-root", required=True)
    service_mode = recording_service.add_mutually_exclusive_group()
    service_mode.add_argument(
        "--catalog-sha256",
        help="group or unprepared mode: the exact catalog digest (a workspace service "
        "serves its subject's active corpus-store catalog and refuses this flag)",
    )
    recording_service.add_argument("--recording-group-id")
    service_mode.add_argument("--native-workspace", action="store_true")
    recording_service.add_argument("--subject-id", default="cole-self")
    recording_service.add_argument("--process-generation", required=True)
    recording_service.add_argument("--build-identity", required=True)
    recording_service.add_argument("--expected-client-build", required=True)
    recording_service.set_defaults(handler=_recording_service)
    recording_continue = recording_commands.add_parser(
        "continue-legacy",
        help="continue an unfinished desktop session in a new explicit native context",
    )
    recording_continue.add_argument("--asset-root")
    recording_continue.add_argument("--session-id", required=True)
    recording_continue.add_argument("--device-id", required=True)
    recording_continue.add_argument("--display-name", required=True)
    recording_continue.add_argument("--capture-host", required=True)
    recording_continue.add_argument("--setup-description", default="")
    recording_continue.set_defaults(handler=_recording_continue_legacy)

    intake = commands.add_parser("intake", help="validate and accept transferred sessions")
    intake_commands = intake.add_subparsers(dest="intake_command", required=True)
    intake_validate = intake_commands.add_parser("validate", help="read-only package validation")
    intake_validate.add_argument("session")
    intake_validate.set_defaults(handler=_intake_validate)
    intake_accept = intake_commands.add_parser("accept", help="atomically accept one session")
    intake_accept.add_argument("--asset-root")
    intake_accept.add_argument("--session-id", required=True)
    intake_accept.set_defaults(handler=_intake_accept)
    intake_scan = intake_commands.add_parser("scan", help="one bounded incoming-directory scan")
    intake_scan.add_argument("--asset-root")
    intake_scan.set_defaults(handler=_intake_scan)
    cli_home_host.register(commands, intake_commands)

    sync = commands.add_parser("sync", help="plan or explicitly execute private Tailnet upload")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)
    sync_plan_parser = sync_commands.add_parser("plan")
    sync_plan_parser.add_argument("session")
    sync_plan_parser.add_argument(
        "--target",
        default=PRIMARY_MINI_ALIAS,
        help="the SSH alias of the Mac mini intake; its address lives in ~/.ssh/config",
    )
    sync_plan_parser.add_argument("--remote-user", required=True)
    sync_plan_parser.add_argument(
        "--remote-root",
        required=True,
        help="the remote account's intake root, <home>/.local/share/cole-voice-lab/incoming",
    )
    sync_plan_parser.add_argument("--identity-file")
    sync_plan_parser.add_argument("--known-hosts-file")
    sync_plan_parser.add_argument("--live-plan", action="store_true")
    sync_plan_parser.add_argument("--execute", action="store_true")
    sync_plan_parser.add_argument("--allow-network", action="store_true")
    sync_plan_parser.add_argument(
        "--approved-plan-sha256",
        help="exact digest printed by a separately reviewed live-plan preview",
    )
    sync_plan_parser.add_argument("--json", action="store_true")
    sync_plan_parser.set_defaults(handler=_sync_plan)

    reference = commands.add_parser("reference", help="review and publish exact voice references")
    reference_commands = reference.add_subparsers(dest="reference_command", required=True)
    reference_review = reference_commands.add_parser(
        "review", help="author human headphone-review evidence"
    )
    reference_review.add_argument("--asset-root")
    reference_review.add_argument("--review-id", required=True)
    reference_review.add_argument("--reviewer-id", default="cole-self")
    reference_review.add_argument("--raw-manifest-sha256", required=True)
    reference_review.add_argument("--prompt-id", required=True)
    reference_review.add_argument("--headphones-used", action="store_true")
    reference_review.add_argument("--transcript-matches", action="store_true")
    reference_review.add_argument("--audio-quality-passed", action="store_true")
    reference_review.add_argument("--finding", action="append", default=[])
    reference_review.set_defaults(handler=_reference_review)
    reference_publish = reference_commands.add_parser(
        "publish", help="publish a byte-identical reviewed reference"
    )
    reference_publish.add_argument("--asset-root")
    reference_publish.add_argument("--reference-id", required=True)
    reference_publish.add_argument("--raw-manifest-sha256", required=True)
    reference_publish.add_argument("--prompt-id", required=True)
    reference_publish.add_argument("--consent-path", required=True)
    reference_publish.add_argument("--consent-sha256", required=True)
    reference_publish.add_argument("--headphone-review-path", required=True)
    reference_publish.add_argument("--headphone-review-sha256", required=True)
    reference_publish.add_argument("--created-by", default="cole")
    reference_publish.add_argument("--provenance-statement", required=True)
    reference_publish.set_defaults(handler=_reference_publish)
    reference_validate = reference_commands.add_parser("validate")
    reference_validate.add_argument("--asset-root")
    reference_validate.add_argument("--reference-id", required=True)
    reference_validate.set_defaults(handler=_reference_validate)

    dataset = commands.add_parser(
        "dataset", help="author gates and publish no-copy dataset evidence"
    )
    dataset_commands = dataset.add_subparsers(dest="dataset_command", required=True)
    dataset_pilot_gate = dataset_commands.add_parser(
        "pilot-gate", help="author separate evidence permitting work beyond the pilot"
    )
    dataset_pilot_gate.add_argument("--asset-root")
    dataset_pilot_gate.add_argument("--gate-id", required=True)
    dataset_pilot_gate.add_argument("--pilot-manifest-sha256", action="append", required=True)
    dataset_pilot_gate.add_argument("--approved-by", default="cole")
    dataset_pilot_gate.add_argument("--approved-at", type=_aware_datetime)
    dataset_pilot_gate.add_argument("--statement", required=True)
    dataset_pilot_gate.add_argument("--approve", action="store_true")
    dataset_pilot_gate.add_argument("--headphone-review-completed", action="store_true")
    dataset_pilot_gate.add_argument("--allow-full-corpus", action="store_true")
    dataset_pilot_gate.set_defaults(handler=_dataset_pilot_gate)

    dataset_build = dataset_commands.add_parser(
        "build", help="build, revalidate, and atomically publish a no-copy dataset"
    )
    dataset_build.add_argument("--asset-root")
    dataset_build.add_argument("--dataset-id", required=True)
    dataset_build.add_argument("--corpus-stage", choices=("pilot", "full-corpus"), required=True)
    dataset_build.add_argument("--selection-file", required=True)
    dataset_build.add_argument(
        "--prompt-suite",
        action="append",
        required=True,
        metavar="SUITE_ID=PATH",
        help="exact prompt-suite JSON; repeat once for each suite used",
    )
    dataset_build.add_argument("--consent-path", required=True)
    dataset_build.add_argument("--consent-sha256", required=True)
    dataset_build.add_argument("--created-by", default="cole")
    dataset_build.add_argument(
        "--minimum-holdout-seconds",
        type=float,
        default=DEFAULT_MINIMUM_HOLDOUT_SECONDS,
    )
    dataset_build.add_argument("--pilot-gate-path")
    dataset_build.add_argument("--pilot-gate-sha256")
    dataset_build.set_defaults(handler=_dataset_build)

    dataset_validate = dataset_commands.add_parser(
        "validate", help="revalidate a published dataset and every evidence binding"
    )
    dataset_validate.add_argument("--asset-root")
    dataset_validate.add_argument("--dataset-id", required=True)
    dataset_validate.set_defaults(handler=_dataset_validate)

    dataset_transfer_plan = dataset_commands.add_parser(
        "transfer-plan", help="publish an exact payload plan without executing a transfer"
    )
    dataset_transfer_plan.add_argument("--asset-root")
    dataset_transfer_plan.add_argument("--plan-id", required=True)
    dataset_transfer_plan.add_argument("--dataset-id", required=True)
    dataset_transfer_plan.add_argument("--created-by", default="cole")
    dataset_transfer_plan.add_argument("--hosted-destination")
    dataset_transfer_plan.add_argument("--hosted-approval-path")
    dataset_transfer_plan.add_argument("--hosted-approval-sha256")
    dataset_transfer_plan.set_defaults(handler=_dataset_transfer_plan)

    models = commands.add_parser("models", help="inspect and explicitly provision candidates")
    model_commands = models.add_subparsers(dest="models_command", required=True)
    models_list = model_commands.add_parser("list")
    _add_registry_arguments(models_list)
    models_list.add_argument("--track", choices=("direct-tts", "voice-conversion"))
    models_list.set_defaults(handler=_models_list)
    models_verify = model_commands.add_parser("verify-registry")
    _add_registry_arguments(models_verify)
    models_verify.set_defaults(handler=_models_verify_registry)
    models_prepare = model_commands.add_parser(
        "prepare", help="show a plan; download only with both execution flags"
    )
    _add_registry_arguments(models_prepare)
    models_prepare.add_argument("--asset-root")
    models_prepare.add_argument("candidate_id")
    models_prepare.add_argument("--profile", choices=("smoke", "quality"), default="smoke")
    models_prepare.add_argument("--base-python")
    models_prepare.add_argument(
        "--reviewed-wheelhouse",
        help="exact local wheel directory required when the runtime lock names reviewed builds",
    )
    models_prepare.add_argument("--lab-wheel", help="reviewed cole-voice-lab wheel")
    models_prepare.add_argument("--lab-wheel-sha256")
    models_prepare.add_argument("--expected-runtime-receipt-sha256")
    models_prepare.add_argument("--execute", action="store_true")
    models_prepare.add_argument("--allow-downloads", action="store_true")
    models_prepare.set_defaults(handler=_models_prepare)
    models_evict = model_commands.add_parser(
        "evict", help="preview or explicitly delete one exact verified model snapshot"
    )
    _add_registry_arguments(models_evict)
    models_evict.add_argument("--asset-root")
    models_evict.add_argument("source_id")
    models_evict.add_argument("--expected-manifest-sha256")
    models_evict.add_argument("--execute", action="store_true")
    models_evict.add_argument("--confirm-evict", action="store_true")
    models_evict.set_defaults(handler=_models_evict)
    models_evict_runtime = model_commands.add_parser(
        "evict-runtime",
        help="preview or explicitly delete one receipt-sealed runtime only",
    )
    _add_registry_arguments(models_evict_runtime)
    models_evict_runtime.add_argument("--asset-root")
    models_evict_runtime.add_argument("runtime_id")
    models_evict_runtime.add_argument("--expected-receipt-sha256")
    models_evict_runtime.add_argument("--execute", action="store_true")
    models_evict_runtime.add_argument("--confirm-evict", action="store_true")
    models_evict_runtime.set_defaults(handler=_models_evict_runtime)

    audition = commands.add_parser(
        "audition", help="plan and sequentially run the checksum-bound candidate bake-off"
    )
    audition_commands = audition.add_subparsers(dest="audition_command", required=True)
    audition_plan_parser = audition_commands.add_parser(
        "plan", help="persist an immutable all-candidate audition matrix"
    )
    _add_registry_arguments(audition_plan_parser)
    audition_plan_parser.add_argument("--asset-root")
    audition_plan_parser.add_argument("--experiment-id", required=True)
    audition_plan_parser.add_argument(
        "--suite", default="evaluation", choices=("evaluation", "evaluation-v1")
    )
    audition_plan_parser.add_argument(
        "--prompt-id", action="append", default=[], help="held-out prompt; defaults to the suite"
    )
    audition_plan_parser.add_argument("--reference-id", action="append", required=True)
    audition_plan_parser.add_argument("--seed", action="append", required=True, type=int)
    audition_plan_parser.add_argument(
        "--source-audio",
        action="append",
        default=[],
        metavar="PROMPT_ID=PATH",
        help="checksum-bound performed source for Seed-VC; repeat for every selected prompt",
    )
    audition_plan_parser.add_argument("--profile", choices=("smoke", "quality"), default="smoke")
    audition_plan_parser.add_argument("--timing-mode", action="append", choices=("cold", "warm"))
    audition_plan_parser.set_defaults(handler=_audition_plan)

    audition_run = audition_commands.add_parser(
        "run", help="preview or explicitly execute prepared trials without network access"
    )
    _add_registry_arguments(audition_run)
    audition_run.add_argument("--asset-root")
    audition_run.add_argument("--experiment-id", required=True)
    audition_run.add_argument(
        "--candidate",
        action="append",
        required=True,
        help="run one candidate at a time on the 16 GiB mini; repeat only deliberately",
    )
    audition_run.add_argument("--trial-id", action="append", default=[])
    audition_run.add_argument(
        "--runtime-receipt",
        action="append",
        default=[],
        metavar="RUNTIME_ID=SHA256",
        help="approved prepare-output runtime receipt; repeat for each selected runtime",
    )
    audition_run.add_argument("--execute", action="store_true")
    audition_run.set_defaults(handler=_audition_run)

    audition_status = audition_commands.add_parser("status", help="read persisted run evidence")
    _add_registry_arguments(audition_status)
    audition_status.add_argument("--asset-root")
    audition_status.add_argument("--experiment-id", required=True)
    audition_status.set_defaults(handler=_audition_status)

    blind = commands.add_parser(
        "blind", help="build, verify, and score evidence-bound listening packages"
    )
    blind_commands = blind.add_subparsers(dest="blind_command", required=True)
    blind_build = blind_commands.add_parser(
        "build", help="derive a complete balanced package from one audition run"
    )
    blind_build.add_argument("--asset-root")
    blind_build.add_argument("--experiment-id", required=True)
    blind_build.add_argument("--review-id", required=True)
    blind_build.add_argument(
        "--track",
        action="append",
        choices=("direct-tts", "voice-conversion"),
        help="review track; defaults to direct-tts and never selects individual candidates",
    )
    blind_build.set_defaults(handler=_blind_build)
    blind_verify = blind_commands.add_parser("verify")
    blind_verify.add_argument("--asset-root")
    blind_verify.add_argument("--review-id", required=True)
    blind_verify.set_defaults(handler=_blind_verify)
    blind_aggregate = blind_commands.add_parser("aggregate")
    blind_aggregate.add_argument("--asset-root")
    blind_aggregate.add_argument("--score-sheet", required=True)
    blind_aggregate.set_defaults(handler=_blind_aggregate)

    promotion = commands.add_parser(
        "promotion", help="validate promotion evidence and export a ChloeSpeech handoff"
    )
    promotion_commands = promotion.add_subparsers(dest="promotion_command", required=True)
    promotion_validate = promotion_commands.add_parser(
        "validate", help="deeply revalidate one schema-authored promotion manifest"
    )
    promotion_validate.add_argument("--asset-root", required=True)
    promotion_validate.add_argument("--manifest", type=Path, required=True)
    promotion_validate.add_argument("--as-of", type=_aware_datetime)
    promotion_validate.set_defaults(handler=_promotion_validate)
    promotion_handoff = promotion_commands.add_parser(
        "handoff", help="validate and write one new canonical ChloeSpeech handoff"
    )
    promotion_handoff.add_argument("--asset-root", required=True)
    promotion_handoff.add_argument("--manifest", type=Path, required=True)
    promotion_handoff.add_argument("--output", type=Path, required=True)
    promotion_handoff.add_argument("--as-of", type=_aware_datetime)
    promotion_handoff.set_defaults(handler=_promotion_handoff)

    consent = commands.add_parser("consent", help="manage machine-readable self-consent")
    consent_commands = consent.add_subparsers(dest="consent_command", required=True)
    consent_install = consent_commands.add_parser("install-pending")
    consent_install.add_argument("--asset-root")
    consent_install.set_defaults(handler=_consent_install)
    consent_preview = consent_commands.add_parser("preview")
    _add_consent_scope_arguments(consent_preview)
    consent_preview.set_defaults(handler=_consent_preview)
    consent_approve = consent_commands.add_parser("approve-self")
    consent_approve.add_argument("--asset-root")
    _add_consent_scope_arguments(consent_approve)
    consent_approve.add_argument("--expected-current-sha256")
    consent_approve.add_argument("--reviewed-scope-sha256", required=True)
    consent_approve.add_argument("--i-am-the-subject-and-authorize", action="store_true")
    consent_approve.set_defaults(handler=_consent_approve)
    consent_status = consent_commands.add_parser("status")
    consent_status.add_argument("--asset-root")
    consent_status.add_argument("--consent-id", default="cole-self-v1")
    consent_status.set_defaults(handler=_consent_status)
    consent_revoke_parser = consent_commands.add_parser("revoke")
    consent_revoke_parser.add_argument("--asset-root")
    consent_revoke_parser.add_argument("--consent-id", default="cole-self-v1")
    consent_revoke_parser.add_argument("--expected-sha256", required=True)
    consent_revoke_parser.add_argument("--confirm-revoke", action="store_true")
    consent_revoke_parser.set_defaults(handler=_consent_revoke)

    vault = commands.add_parser(
        "vault", help="bind, approve and operate the library's encrypted vault"
    )
    vault_commands = vault.add_subparsers(dest="vault_command", required=True)

    def _vault_arguments(
        parser: argparse.ArgumentParser, *, destination: bool = True
    ) -> argparse.ArgumentParser:
        parser.add_argument("--asset-root")
        if destination:
            parser.add_argument(
                "--destination",
                type=_destination_argument,
                default=DEFAULT_VAULT_DESTINATION,
                help="which of the library's vault destinations (default: "
                f"{DEFAULT_VAULT_DESTINATION}, a single-vault host's one destination)",
            )
        return parser

    def _vault_destination_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--approval-id", required=True)
        parser.add_argument("--endpoint", required=True)
        parser.add_argument("--tailnet-suffix", required=True)
        parser.add_argument("--node-stable-id", required=True)
        parser.add_argument("--namespace", required=True)
        parser.add_argument(
            "--classes", nargs="+", required=True, choices=VAULT_FILE_CLASSES, metavar="CLASS"
        )
        parser.add_argument("--approved-at", type=_aware_datetime)
        parser.add_argument("--expires-at", type=_aware_datetime, required=True)
        parser.add_argument("--approved-by", required=True)
        parser.add_argument("--statement", required=True)

    vault_init = _vault_arguments(
        vault_commands.add_parser("init", help="record which library this host holds"),
        destination=False,
    )
    vault_init.add_argument("--library-id", help="adopt an existing vault library's id")
    vault_init.set_defaults(handler=_vault_init)

    vault_approve = _vault_arguments(
        vault_commands.add_parser(
            "approve", help="preview, then author, a standing destination approval"
        )
    )
    _vault_destination_arguments(vault_approve)
    vault_approve.add_argument("--preview", action="store_true")
    vault_approve.add_argument("--reviewed-approval-sha256", type=_sha256_argument)
    vault_approve.add_argument("--i-approve-this-destination", action="store_true")
    vault_approve.set_defaults(handler=_vault_approve)

    vault_bind = _vault_arguments(
        vault_commands.add_parser("bind", help="bind this library to one vault")
    )
    vault_bind.add_argument("--backend", required=True, choices=VAULT_BACKENDS)
    vault_bind.add_argument("--endpoint", required=True)
    vault_bind.add_argument("--namespace", required=True)
    vault_bind.add_argument(
        "--classes", nargs="+", required=True, choices=VAULT_FILE_CLASSES, metavar="CLASS"
    )
    vault_bind.add_argument("--approval-id")
    vault_bind.add_argument("--approval-sha256", type=_sha256_argument)
    vault_bind.add_argument("--replace", action="store_true")
    vault_bind.add_argument("--reset-state", action="store_true")
    vault_bind.set_defaults(handler=_vault_bind)

    vault_show = _vault_arguments(
        vault_commands.add_parser("show", help="the binding, its approval and this Mac's records")
    )
    vault_show.add_argument("--json", action="store_true")
    vault_show.set_defaults(handler=_vault_show)

    vault_status = _vault_arguments(
        vault_commands.add_parser("status", help="one line, as the recording app shows it")
    )
    vault_status.add_argument("--json", action="store_true")
    vault_status.set_defaults(handler=_vault_status)

    vault_push = _vault_arguments(
        vault_commands.add_parser("push", help="copy the library's current files to the vault")
    )
    vault_push.add_argument("--dry-run", action="store_true")
    vault_push.add_argument(
        "--full", action="store_true", help="re-upload every file instead of reusing chunks"
    )
    vault_push.set_defaults(handler=_vault_push)

    vault_verify = _vault_arguments(
        vault_commands.add_parser("verify", help="download and check every chunk of a generation")
    )
    vault_verify.add_argument("--generation", type=_positive_number)
    vault_verify.add_argument("--path", dest="paths", action="append", default=[])
    vault_verify.add_argument(
        "--class", dest="classes", action="append", default=[], choices=VAULT_FILE_CLASSES
    )
    vault_verify.set_defaults(handler=_vault_verify)

    vault_restore = _vault_arguments(
        vault_commands.add_parser("restore", help="restore one generation into an empty root")
    )
    vault_restore.add_argument("--target", required=True)
    vault_restore.add_argument("--runtime-root")
    restore_generation = vault_restore.add_mutually_exclusive_group(required=True)
    restore_generation.add_argument("--accept-latest", action="store_true")
    restore_generation.add_argument("--generation", type=_positive_number)
    vault_restore.add_argument("--manifest-sha256", type=_sha256_argument)
    vault_restore.add_argument("--backend", choices=VAULT_BACKENDS, default="tailnet-vault")
    vault_restore.add_argument("--endpoint", help="restore onto a Mac with no binding")
    vault_restore.add_argument("--namespace")
    vault_restore.add_argument("--node-stable-id")
    vault_restore.add_argument("--tailnet-suffix")
    vault_restore.set_defaults(handler=_vault_restore)

    vault_drill = _vault_arguments(
        vault_commands.add_parser(
            "drill", help="restore a generation into an empty root and re-hash every file"
        )
    )
    vault_drill.add_argument("--target", required=True)
    drill_generation = vault_drill.add_mutually_exclusive_group(required=True)
    drill_generation.add_argument("--accept-latest", action="store_true")
    drill_generation.add_argument("--generation", type=_positive_number)
    vault_drill.add_argument("--manifest-sha256", type=_sha256_argument)
    vault_drill.set_defaults(handler=_vault_drill)

    vault_audit = _vault_arguments(
        vault_commands.add_parser("audit", help="hold the vault's access log against this Mac's")
    )
    vault_audit.add_argument("--access-log", required=True)
    vault_audit.add_argument("--namespace")
    vault_audit.set_defaults(handler=_vault_audit)

    vault_key = vault_commands.add_parser(
        "key", help="the library key (login Keychain on macOS, systemd credential on Linux)"
    )
    vault_key_commands = vault_key.add_subparsers(dest="vault_key_command", required=True)
    vault_key_create = _vault_arguments(
        vault_key_commands.add_parser("create", help="create the key and its recovery sheet"),
        destination=False,
    )
    vault_key_create.add_argument("--sheet-out")
    vault_key_create.add_argument("--print-to-terminal", action="store_true")
    vault_key_create.set_defaults(handler=_vault_key_create)
    vault_key_import = _vault_arguments(
        vault_key_commands.add_parser("import", help="restore the key from its recovery sheet"),
        destination=False,
    )
    vault_key_import.add_argument("--sheet", required=True, help="a sheet file, or - for stdin")
    vault_key_import.set_defaults(handler=_vault_key_import)
    vault_key_show = _vault_arguments(
        vault_key_commands.add_parser("show", help="the key's fingerprint, never the key"),
        destination=False,
    )
    vault_key_show.set_defaults(handler=_vault_key_show)

    vault_token = vault_commands.add_parser(
        "token", help="one destination's vault tokens (login Keychain or systemd credentials)"
    )
    vault_token_commands = vault_token.add_subparsers(dest="vault_token_command", required=True)
    vault_token_create = _vault_arguments(
        vault_token_commands.add_parser("create", help="create both tokens; print only hashes")
    )
    vault_token_create.add_argument("--namespace")
    vault_token_create.set_defaults(handler=_vault_token_create)
    vault_token_import = _vault_arguments(
        vault_token_commands.add_parser("import", help="keep one token read from standard input")
    )
    vault_token_import.add_argument("--scope", required=True, choices=("write", "read"))
    vault_token_import.set_defaults(handler=_vault_token_import)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    handler = cast(Command, options.handler)
    try:
        return handler(options)
    except (
        VoiceLabError,
        ValidationError,
        FileNotFoundError,
        PermissionError,
        subprocess.SubprocessError,
    ) as error:
        # A named failure (``DOMAIN.FAILURE``) leads with its code, so callers need not parse
        # prose to tell one refusal from another.
        print(f"voice-lab: {_error_text(error)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
