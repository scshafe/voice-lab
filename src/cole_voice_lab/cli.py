"""Operator CLI. Mutating and network actions are explicit subcommands/flags."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, JsonValue, ValidationError

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
    require_corpus_eligible_package,
)
from .doctor import run_doctor
from .errors import VoiceLabError
from .intake import accept_capture_session, validate_capture_package
from .models import (
    PROMPT_SUITE_V1,
    CaptureDevice,
    ConsentRecord,
    Prompt,
    PromptSuite,
    ReferenceEvidence,
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


def _asset_root(value: str | None) -> Path:
    configured = value or os.environ.get("COLE_VOICE_LAB_ASSET_ROOT")
    if not configured:
        raise VoiceLabError("an explicit --asset-root or COLE_VOICE_LAB_ASSET_ROOT is required")
    return Path(configured).expanduser()


def _read_bounded_contract[ContractT: BaseModel](
    path: Path, model_type: type[ContractT]
) -> ContractT:
    """Read one strict operator-supplied JSON contract through a bounded descriptor."""

    source = path.expanduser()
    with open_regular_no_follow(source) as stream:
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


def _prompts_show(options: argparse.Namespace) -> int:
    suite = load_builtin_prompt_suite(PROMPT_SHOW_RESOURCES[options.suite])
    if options.json:
        _json_print(suite.model_dump(mode="json"))
        return 0
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
    return {
        "subject_id": corpus.subject_id,
        "catalog_suite_id": corpus.catalog_suite_id,
        "catalog_sha256": corpus.catalog_sha256,
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
    from .finetune import select_finetune_corpus

    layout = AssetLayout.validate(_asset_root(options.asset_root))
    catalog = load_builtin_prompt_suite(f"{options.catalog}.json")
    corpus = select_finetune_corpus(layout, catalog, options.subject_id)
    if not corpus.complete and not options.allow_missing:
        raise VoiceLabError(
            "the corpus is incomplete (missing: "
            + ", ".join(corpus.missing_prompt_ids)
            + "; unknown: "
            + ", ".join(corpus.unknown_record_ids)
            + "); record the missing prompts or pass --allow-missing"
        )
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
    from .finetune import describe_finetune_runtime, materialize_finetune_dataset, run_f5_finetune

    layout, corpus = _finetune_select(options)
    runtime = describe_finetune_runtime(Path(options.runtime_root))
    run_id = _finetune_run_id(options)
    run_root = f"{options.output_root}/{run_id}"
    dataset = materialize_finetune_dataset(
        layout, corpus, f"{run_root}/dataset", ffmpeg=Path(options.ffmpeg)
    )
    run = run_f5_finetune(
        layout,
        dataset,
        runtime,
        _finetune_hyperparameters(options),
        run_root,
        run_id=run_id,
        subject_id=corpus.subject_id,
        catalog_sha256=corpus.catalog_sha256,
    )
    finetuned = _finetune_synthesize_one(
        layout,
        options,
        dataset,
        runtime,
        run_root=run_root,
        label="finetuned",
        checkpoint=layout.root / run_root / "checkpoints" / "model_last.pt",
        use_ema=not options.no_ema,
    )
    base = _finetune_synthesize_one(
        layout,
        options,
        dataset,
        runtime,
        run_root=run_root,
        label="base",
        checkpoint=runtime.pretrained_path,
        use_ema=True,
    )
    _json_print(
        {
            "run_id": run_id,
            "run_root": run.run_root,
            "status": run.status,
            "corpus": _finetune_corpus_summary(corpus),
            "checkpoints": [item.model_dump(mode="json") for item in run.checkpoints],
            "samples": {
                "finetuned": [item.relative_path for item in finetuned.samples],
                "base": [item.relative_path for item in base.samples],
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
    catalog = load_builtin_prompt_suite(f"{options.catalog}.json")
    baseline = (
        None if options.baseline is None else load_builtin_prompt_suite(f"{options.baseline}.json")
    )
    try:
        report = prompt_coverage_report(layout, options.subject_id, catalog, baseline)
    except PromptCoverageProjectionError as error:
        print(f"voice-lab: {error}", file=sys.stderr)
        return 1
    if options.json:
        _json_print(report.to_json())
    else:
        print(
            f"{report.covered_prompt_count}/{report.total_prompt_count} prompts covered · "
            f"catalog {report.catalog_sha256}"
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

    A workspace service serves its subject's active corpus-store catalog; group mode still
    names its group's digest. Returns the service's exit status (errors as that ``main``).
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

    prompts = commands.add_parser("prompts", help="inspect bundled prompt suites")
    prompt_commands = prompts.add_subparsers(dest="prompts_command", required=True)
    prompt_show = prompt_commands.add_parser("show")
    prompt_show.add_argument("suite", choices=tuple(PROMPT_SHOW_RESOURCES))
    prompt_show.add_argument("--json", action="store_true")
    prompt_show.set_defaults(handler=_prompts_show)
    builtin_suites = tuple(name.removesuffix(".json") for name in BUILTIN_PROMPT_SUITE_SHA256)
    coverage_report = prompt_commands.add_parser(
        "coverage-report",
        help="read-only exact coverage of a built-in catalog; exit 1 on unexplained losses",
    )
    coverage_report.add_argument("--asset-root")
    coverage_report.add_argument("--subject-id", required=True)
    coverage_report.add_argument("--catalog", required=True, choices=builtin_suites)
    coverage_report.add_argument("--baseline", choices=builtin_suites)
    coverage_report.add_argument("--json", action="store_true")
    coverage_report.set_defaults(handler=_prompts_coverage_report)

    finetune = commands.add_parser(
        "finetune", help="fine-tune a TTS model from the latest kept take of every prompt"
    )
    finetune_commands = finetune.add_subparsers(dest="finetune_command", required=True)

    def _finetune_corpus_arguments(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--asset-root")
        parser.add_argument("--subject-id", default="cole-self")
        parser.add_argument("--catalog", required=True, choices=builtin_suites)
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
    recording_service.add_argument(
        "--catalog-sha256",
        help="group or unprepared mode: the exact catalog digest (a workspace service "
        "serves its subject's active corpus-store catalog)",
    )
    recording_service.add_argument("--recording-group-id")
    recording_service.add_argument("--native-workspace", action="store_true")
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
        print(f"voice-lab: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
