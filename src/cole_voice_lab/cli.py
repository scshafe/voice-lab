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
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast, get_args

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

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
from .sync import (
    DEFAULT_REMOTE_ROOT,
    PRIMARY_MINI_TARGET,
    build_sync_plan,
    execute_sync_plan,
)

if TYPE_CHECKING:
    from .corpus_store import CatalogRecord, CorpusStore, CorpusStoreCode, PromptRecord
    from .recording_contracts import CatalogReference, CatalogSnapshot

Command = Callable[[argparse.Namespace], int]
MAX_CLI_DATASET_JSON_BYTES = 16 * 1024 * 1024
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
    from .corpus_store import CorpusStoreError

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
    through the library's corpus store (``open_corpus_store(create=False)``: the store's
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
    from .corpus_store import CorpusStoreError, open_corpus_store

    layout = AssetLayout.validate(_asset_root(asset_root))
    try:
        with open_corpus_store(layout, create=False) as store:
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

    layout, corpus = _finetune_select(options)
    run_id = _finetune_run_id(options)
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
    from .finetune import FineTuneCorpus, FineTuneDataset

    dataset_directory = layout.root / run_root / "dataset"
    dataset = _read_bounded_contract(dataset_directory / "dataset.json", FineTuneDataset)
    corpus = _read_bounded_contract(dataset_directory / "corpus.json", FineTuneCorpus)
    if canonical_sha256(corpus) != dataset.corpus_sha256:
        raise VoiceLabError(f"{dataset_directory}: corpus.json does not match dataset.json")
    return corpus, dataset


def _finetune_train(options: argparse.Namespace) -> int:
    from .finetune import describe_finetune_runtime, run_f5_finetune

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    run_root = f"{options.output_root}/{options.run_id}"
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


def _finetune_synthesize(options: argparse.Namespace) -> int:
    from .finetune import describe_finetune_runtime

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    run_root = f"{options.output_root}/{options.run_id}"
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
    resolved = _resolve_catalog(options.catalog, options.asset_root, BUILTIN_CATALOG_RESOURCES)
    run_id = _finetune_run_id(options)
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
        from .corpus_store import CorpusStoreError

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


def _open_catalog_store(layout: AssetLayout, *, create: bool = False) -> CorpusStore:
    from .corpus_store import open_corpus_store

    return open_corpus_store(layout, create=create)


def _published(
    store: CorpusStore, catalog_id: str, version: int | None
) -> tuple[CatalogReference, CatalogSnapshot]:
    reference = store.resolve_reference(catalog_id, version)
    return reference, store.materialize(reference)


def _active_reference(store: CorpusStore, subject_id: str) -> CatalogReference | None:
    """The subject's active version, or ``None``; a damaged active snapshot still fails."""
    from .corpus_store import CorpusStoreError

    try:
        return store.active_catalog(subject_id)[1]
    except CorpusStoreError as error:
        if error.code != "CORPUS.NO_ACTIVE_CATALOG":
            raise
        return None


def _init_refusal(store: CorpusStore, subject_id: str) -> str | None:
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
    from .corpus_store import (
        CORPUS_DATABASE_NAME,
        CORPUS_DIRECTORY,
        CorpusStoreError,
        catalog_definition,
        validated_subject_id,
        validation_problems,
    )

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
    with _open_catalog_store(layout, create=True) as store:
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
            "store": f"provenance/{CORPUS_DIRECTORY}/{CORPUS_DATABASE_NAME}",
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
    with _open_catalog_store(layout) as store:
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
    with _open_catalog_store(layout) as store:
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
    with _open_catalog_store(layout) as store:
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
    with _open_catalog_store(layout) as store:
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
    with _open_catalog_store(layout) as store:
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
    with _open_catalog_store(layout) as store:
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
    with _open_catalog_store(layout) as store:
        record = store.retire_prompt(options.prompt_id)
    _json_print(_prompt_record_json(record))
    return 0


def _require_standard_set_absent(store: CorpusStore, name: str, first_prompt_id: str) -> None:
    from .corpus_store import CorpusStoreError

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
    with _open_catalog_store(layout) as store:
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
    with _open_catalog_store(layout) as store:
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
    with _open_catalog_store(layout) as store:
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
    with _open_catalog_store(layout) as store:
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
    with _open_catalog_store(layout) as store:
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

    sync = commands.add_parser("sync", help="plan or explicitly execute private Tailnet upload")
    sync_commands = sync.add_subparsers(dest="sync_command", required=True)
    sync_plan_parser = sync_commands.add_parser("plan")
    sync_plan_parser.add_argument("session")
    sync_plan_parser.add_argument("--target", default=PRIMARY_MINI_TARGET)
    sync_plan_parser.add_argument("--remote-user", default="cole")
    sync_plan_parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
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
