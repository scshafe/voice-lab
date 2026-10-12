from __future__ import annotations

import json
import os
import stat
import wave
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.capture import expected_recording_paths, finalize_capture_session
from cole_voice_lab.datasets import (
    CategoryAccounting,
    DatasetEntry,
    DatasetManifest,
    DatasetSelection,
    FineTuningTransferPlan,
    HoldoutGate,
    HostedTransferApproval,
    PilotGateApproval,
    build_dataset_manifest,
    build_transfer_plan,
    publish_dataset_manifest,
    publish_transfer_plan,
    validate_dataset_sources,
    validate_published_dataset,
    validate_transfer_plan,
)
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.intake import MIN_FREE_BYTES, IntakeReceipt, accept_capture_session
from cole_voice_lab.models import CaptureDevice, Prompt, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.references import HeadphoneReviewEvidence, load_accepted_utterance

NOW = datetime(2026, 8, 17, 18, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _restore_test_permissions(tmp_path: Path) -> Iterator[None]:
    yield
    for root, directories, files in os.walk(tmp_path):
        for name in directories:
            os.chmod(Path(root) / name, 0o700)
        for name in files:
            os.chmod(Path(root) / name, 0o600)


@dataclass(frozen=True)
class CorpusFixture:
    layout: AssetLayout
    receipts: dict[str, IntakeReceipt]
    suites: dict[str, PromptSuite]
    selections: tuple[DatasetSelection, ...]
    gate_path: str
    gate_sha256: str


def _write_wav(path: Path) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    samples = [1_000_000, -1_000_000] * 480
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(3)
        writer.setframerate(48_000)
        writer.writeframes(b"".join(value.to_bytes(3, "little", signed=True) for value in samples))


def _suite(
    *,
    suite_id: str,
    purpose: str,
    prompt_id: str,
    text: str,
    category: str,
) -> PromptSuite:
    held_out = purpose == "held-out-evaluation"
    return PromptSuite.model_validate(
        {
            "suite_id": suite_id,
            "purpose": purpose,
            "prompts": (
                Prompt(
                    prompt_id=f"room-{prompt_id}",
                    text="Remain silent.",
                    category="room-tone",
                    style="Silent room tone.",
                    holdout=held_out,
                    expected_seconds=1.0,
                ),
                Prompt.model_validate(
                    {
                        "prompt_id": prompt_id,
                        "text": text,
                        "category": category,
                        "style": "Exact corpus delivery.",
                        "holdout": held_out,
                        "expected_seconds": 1.0,
                    }
                ),
            ),
        }
    )


def _accept(
    layout: AssetLayout, *, session_id: str, suite: PromptSuite, subject_id: str = "cole-self"
) -> IntakeReceipt:
    incoming = layout.directory("incoming") / session_id
    incoming.mkdir(mode=0o700)
    for relative in expected_recording_paths(suite):
        _write_wav(incoming / relative)
    finalize_capture_session(
        incoming,
        session_id=session_id,
        subject_id=subject_id,
        prompt_suite=suite,
        device=CaptureDevice(device_id="4", display_name="miniDSP UMIK-1"),
        captured_at=NOW,
        finalized_at=NOW,
        capture_host="coles-macbook-pro",
    )
    return accept_capture_session(
        layout,
        incoming,
        available_bytes=MIN_FREE_BYTES,
        clock=lambda: NOW,
    )


def _review(
    layout: AssetLayout,
    receipt: IntakeReceipt,
    prompt_id: str,
) -> tuple[str, str]:
    source = load_accepted_utterance(
        layout,
        raw_manifest_sha256=receipt.manifest_sha256,
        prompt_id=prompt_id,
    )
    record = HeadphoneReviewEvidence(
        review_id=f"review-{receipt.session_id}",
        reviewer_id="cole",
        session_id=receipt.session_id,
        prompt_id=prompt_id,
        source_sha256=source.binding.raw_audio_sha256,
        reviewed_at=NOW + timedelta(seconds=1),
        headphones_used=True,
        transcript_matches=True,
        audio_quality_passed=True,
        findings=("Transcript and audio quality passed on closed-back headphones.",),
    )
    directory = layout.directory("provenance") / "headphone-reviews"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / f"{record.review_id}.json"
    digest = write_canonical_json(path, record)
    return path.relative_to(layout.root).as_posix(), digest


def _corpus(tmp_path: Path, *, subject_id: str = "cole-self") -> CorpusFixture:
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    suites = {
        "train-suite": _suite(
            suite_id="train-suite",
            purpose="capture-corpus",
            prompt_id="train-neutral-01",
            text="The training sentence remains exact.",
            category="neutral",
        ),
        "validation-suite": _suite(
            suite_id="validation-suite",
            purpose="capture-corpus",
            prompt_id="validation-technical-01",
            text="Validation evidence remains separate.",
            category="technical",
        ),
        "holdout-suite": _suite(
            suite_id="holdout-suite",
            purpose="held-out-evaluation",
            prompt_id="holdout-expressive-01",
            text="The permanent holdout is never used for training.",
            category="expressive",
        ),
    }
    session_and_prompt = (
        ("train-session", "train-suite", "train-neutral-01", "train"),
        (
            "validation-session",
            "validation-suite",
            "validation-technical-01",
            "validation",
        ),
        (
            "holdout-session",
            "holdout-suite",
            "holdout-expressive-01",
            "permanent-holdout",
        ),
    )
    receipts: dict[str, IntakeReceipt] = {}
    selections: list[DatasetSelection] = []
    for session_id, suite_id, prompt_id, split in session_and_prompt:
        receipt = _accept(
            layout, session_id=session_id, suite=suites[suite_id], subject_id=subject_id
        )
        receipts[session_id] = receipt
        review_path, review_sha256 = _review(layout, receipt, prompt_id)
        selections.append(
            DatasetSelection.model_validate(
                {
                    "raw_manifest_sha256": receipt.manifest_sha256,
                    "prompt_id": prompt_id,
                    "split": split,
                    "headphone_review_path": review_path,
                    "headphone_review_sha256": review_sha256,
                }
            )
        )

    gate = PilotGateApproval(
        gate_id="pilot-gate-v1",
        pilot_manifest_sha256s=(receipts["train-session"].manifest_sha256,),
        approved=True,
        headphone_review_completed=True,
        full_corpus_capture_allowed=True,
        approved_at=NOW + timedelta(seconds=1),
        approved_by="cole",
        statement="Pilot QC and headphone review passed; full corpus may proceed.",
    )
    gate_directory = layout.directory("provenance") / "pilot-gates"
    gate_directory.mkdir(mode=0o700, exist_ok=True)
    gate_file = gate_directory / "pilot-gate-v1.json"
    gate_sha256 = write_canonical_json(gate_file, gate)
    return CorpusFixture(
        layout=layout,
        receipts=receipts,
        suites=suites,
        selections=tuple(selections),
        gate_path=gate_file.relative_to(layout.root).as_posix(),
        gate_sha256=gate_sha256,
    )


def _manifest(fixture: CorpusFixture, *, dataset_id: str = "cole-corpus-v1") -> DatasetManifest:
    return build_dataset_manifest(
        fixture.layout,
        dataset_id=dataset_id,
        corpus_stage="full-corpus",
        selections=fixture.selections,
        prompt_suites=fixture.suites,
        created_by="cole",
        minimum_holdout_seconds=0.01,
        pilot_gate_path=fixture.gate_path,
        pilot_gate_sha256=fixture.gate_sha256,
        clock=lambda: NOW + timedelta(seconds=2),
    )


def test_build_and_publish_no_copy_dataset_with_exact_accounting(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    raw_before = {
        path.relative_to(fixture.layout.root).as_posix(): (sha256_file(path), path.stat().st_mode)
        for path in fixture.layout.directory("raw").glob("*/audio/*.wav")
    }
    manifest = _manifest(fixture)

    first = publish_dataset_manifest(fixture.layout, manifest)
    second = publish_dataset_manifest(fixture.layout, manifest)
    # No consent record is bound or read (D-050): the manifest names none.
    assert manifest.consent is None
    assert b"consent" not in (first.directory / "manifest.json").read_bytes()
    assert not (fixture.layout.root / "provenance/consents").exists()

    assert first == second
    assert {entry.split for entry in manifest.entries} == {
        "train",
        "validation",
        "permanent-holdout",
    }
    assert [row.category for row in manifest.category_accounting] == [
        "expressive",
        "neutral",
        "technical",
    ]
    assert manifest.holdout_gate.passed is True
    assert manifest.raw_audio_copied is False
    assert [child.name for child in first.directory.iterdir()] == ["manifest.json"]
    assert stat.S_IMODE((first.directory / "manifest.json").stat().st_mode) == 0o400
    assert all(entry.source.transcript == entry.prompt.text for entry in manifest.entries)
    raw_after = {
        path.relative_to(fixture.layout.root).as_posix(): (sha256_file(path), path.stat().st_mode)
        for path in fixture.layout.directory("raw").glob("*/audio/*.wav")
    }
    assert raw_after == raw_before
    assert validate_published_dataset(fixture.layout, first.directory).manifest == manifest


def test_dataset_enforces_minimum_permanent_holdout(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    with pytest.raises(ContractError, match="permanent holdout"):
        build_dataset_manifest(
            fixture.layout,
            dataset_id="short-holdout",
            corpus_stage="full-corpus",
            selections=fixture.selections,
            prompt_suites=fixture.suites,
            created_by="cole",
            minimum_holdout_seconds=600.0,
            pilot_gate_path=fixture.gate_path,
            pilot_gate_sha256=fixture.gate_sha256,
            clock=lambda: NOW + timedelta(seconds=2),
        )


def test_dataset_builder_rejects_invalid_threshold_empty_input_and_duplicate_suites(
    tmp_path: Path,
) -> None:
    fixture = _corpus(tmp_path)
    common: dict[str, Any] = {
        "layout": fixture.layout,
        "dataset_id": "invalid-builder-input",
        "corpus_stage": "pilot",
        "prompt_suites": fixture.suites,
        "created_by": "cole",
        "clock": lambda: NOW + timedelta(seconds=2),
    }
    with pytest.raises(ValueError, match="must be positive"):
        build_dataset_manifest(
            **common,
            selections=fixture.selections,
            minimum_holdout_seconds=0.0,
        )
    with pytest.raises(ContractError, match="cannot be empty"):
        build_dataset_manifest(
            **common,
            selections=(),
            minimum_holdout_seconds=0.01,
        )
    with pytest.raises(ContractError, match="duplicate suite IDs"):
        build_dataset_manifest(
            **{
                **common,
                "prompt_suites": {
                    "first": fixture.suites["train-suite"],
                    "second": fixture.suites["train-suite"],
                },
            },
            selections=fixture.selections,
            minimum_holdout_seconds=0.01,
        )


def test_dataset_builder_refuses_physical_test_suites_before_any_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.datasets as datasets_module
    import cole_voice_lab.intake as intake_module
    import cole_voice_lab.references as references_module

    # Guards #5 (intake) and #6 (accepted-utterance loader) refuse this evidence first;
    # they are disabled here so guard #8 is proven as an independent last layer.
    monkeypatch.setattr(intake_module, "_require_intake_root", lambda *_: None)
    monkeypatch.setattr(references_module, "require_corpus_eligible_package", lambda *_: None)
    layout = AssetLayout.initialize(tmp_path / "private" / "voice-assets")
    suite = PromptSuite(
        schema_version="cole-voice-lab.prompt-suite/v2",
        suite_id="cole-physical-test-synthetic",
        purpose="physical-test",
        prompts=(
            Prompt(
                prompt_id="room-physical",
                instructions="Remain silent.",
                category="room-tone",
                style="Silent room tone.",
                labels=("test",),
                expected_seconds=1.0,
            ),
            Prompt(
                prompt_id="spoken-physical",
                text="A synthetic physical-test sentence.",
                category="neutral",
                style="Exact delivery.",
                labels=("test",),
                expected_seconds=1.0,
            ),
        ),
    )
    receipt = _accept(layout, session_id="native-synthetic-physical", suite=suite)
    review_path, review_sha256 = _review(layout, receipt, "spoken-physical")
    selection = DatasetSelection.model_validate(
        {
            "raw_manifest_sha256": receipt.manifest_sha256,
            "prompt_id": "spoken-physical",
            "split": "train",
            "headphone_review_path": review_path,
            "headphone_review_sha256": review_sha256,
        }
    )
    monkeypatch.setattr(
        datasets_module,
        "DatasetEntry",
        lambda **_: pytest.fail("physical-test refusal must precede DatasetEntry"),
    )

    def build() -> None:
        build_dataset_manifest(
            layout,
            dataset_id="physical-test-leak",
            corpus_stage="pilot",
            selections=(selection,),
            prompt_suites={suite.suite_id: suite},
            created_by="cole",
            clock=lambda: NOW + timedelta(seconds=2),
        )

    with pytest.raises(ContractError, match="physical-test prompt suites"):
        build()
    monkeypatch.undo()
    with pytest.raises(ContractError, match="physical-test capture packages"):
        build()


def test_full_corpus_requires_separate_pilot_gate(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    with pytest.raises(SecurityError, match="prohibited before the pilot gate"):
        build_dataset_manifest(
            fixture.layout,
            dataset_id="no-gate",
            corpus_stage="full-corpus",
            selections=fixture.selections,
            prompt_suites=fixture.suites,
            created_by="cole",
            minimum_holdout_seconds=0.01,
            clock=lambda: NOW + timedelta(seconds=2),
        )
    with pytest.raises(SecurityError, match="supplied together"):
        build_dataset_manifest(
            fixture.layout,
            dataset_id="half-gate",
            corpus_stage="full-corpus",
            selections=fixture.selections,
            prompt_suites=fixture.suites,
            created_by="cole",
            minimum_holdout_seconds=0.01,
            pilot_gate_path=fixture.gate_path,
        )


def test_pilot_dataset_can_be_built_without_full_corpus_gate(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    manifest = build_dataset_manifest(
        fixture.layout,
        dataset_id="pilot-dataset",
        corpus_stage="pilot",
        selections=fixture.selections,
        prompt_suites=fixture.suites,
        created_by="cole",
        minimum_holdout_seconds=0.01,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    assert manifest.pilot_gate is None


def test_dataset_rejects_transcript_or_holdout_suite_mismatch(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    wrong_suites = dict(fixture.suites)
    train = wrong_suites["train-suite"]
    wrong_suites["train-suite"] = train.model_copy(
        update={
            "prompts": (
                train.prompts[0],
                train.prompts[1].model_copy(update={"text": "Changed transcript."}),
            )
        }
    )
    with pytest.raises(ContractError, match="exactly match"):
        build_dataset_manifest(
            fixture.layout,
            dataset_id="wrong-transcript",
            corpus_stage="full-corpus",
            selections=fixture.selections,
            prompt_suites=wrong_suites,
            created_by="cole",
            minimum_holdout_seconds=0.01,
            pilot_gate_path=fixture.gate_path,
            pilot_gate_sha256=fixture.gate_sha256,
            clock=lambda: NOW + timedelta(seconds=2),
        )

    selections = list(fixture.selections)
    selections[2] = selections[2].model_copy(update={"split": "train"})
    with pytest.raises(ValidationError, match="held-out prompts"):
        build_dataset_manifest(
            fixture.layout,
            dataset_id="holdout-leak",
            corpus_stage="full-corpus",
            selections=selections,
            prompt_suites=fixture.suites,
            created_by="cole",
            minimum_holdout_seconds=0.01,
            pilot_gate_path=fixture.gate_path,
            pilot_gate_sha256=fixture.gate_sha256,
            clock=lambda: NOW + timedelta(seconds=2),
        )


@pytest.mark.parametrize("overlap", ["session", "prompt", "transcript"])
def test_dataset_contract_rejects_overlap_across_splits(tmp_path: Path, overlap: str) -> None:
    fixture = _corpus(tmp_path)
    data = _manifest(fixture).model_dump(mode="python")
    train = data["entries"][0]
    validation = data["entries"][1]
    if overlap == "session":
        validation["source"]["session_id"] = train["source"]["session_id"]
        validation["headphone_review"]["record"]["session_id"] = train["source"]["session_id"]
    elif overlap == "prompt":
        validation["source"]["prompt_id"] = train["source"]["prompt_id"]
        validation["prompt"]["prompt_id"] = train["source"]["prompt_id"]
        validation["prompt_suite"]["prompts"][1]["prompt_id"] = train["source"]["prompt_id"]
        validation["headphone_review"]["record"]["prompt_id"] = train["source"]["prompt_id"]
    else:
        validation["source"]["transcript"] = train["source"]["transcript"]
        validation["prompt"]["text"] = train["source"]["transcript"]
        validation["prompt_suite"]["prompts"][1]["text"] = train["source"]["transcript"]
    validation["prompt_suite_sha256"] = canonical_sha256(
        PromptSuite.model_validate(validation["prompt_suite"])
    )
    with pytest.raises(ValidationError, match="overlap across splits"):
        DatasetManifest.model_validate(data)


def test_dataset_requires_bound_passing_review(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    selection = fixture.selections[0]
    review_file = fixture.layout.root / selection.headphone_review_path
    review_data = json.loads(review_file.read_text())
    review_data["source_sha256"] = "f" * 64
    changed_digest = write_canonical_json(review_file, review_data)
    changed = selection.model_copy(update={"headphone_review_sha256": changed_digest})
    with pytest.raises(SecurityError, match="does not bind"):
        build_dataset_manifest(
            fixture.layout,
            dataset_id="bad-review",
            corpus_stage="full-corpus",
            selections=(changed, *fixture.selections[1:]),
            prompt_suites=fixture.suites,
            created_by="cole",
            minimum_holdout_seconds=0.01,
            pilot_gate_path=fixture.gate_path,
            pilot_gate_sha256=fixture.gate_sha256,
            clock=lambda: NOW + timedelta(seconds=2),
        )


def test_dataset_publication_collision_and_stale_source_fail_closed(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    manifest = _manifest(fixture)
    published = publish_dataset_manifest(fixture.layout, manifest)
    collision = manifest.model_copy(update={"created_by": "someone-else"})
    with pytest.raises(SecurityError, match="collision"):
        publish_dataset_manifest(fixture.layout, collision)

    source_path = fixture.layout.root / manifest.entries[0].source.raw_audio_path
    os.chmod(source_path, 0o600)
    source_path.write_bytes(source_path.read_bytes() + b"tamper")
    with pytest.raises(SecurityError, match="tampered"):
        validate_dataset_sources(fixture.layout, published.manifest)


def test_a_dataset_is_of_the_pinned_subject_only(tmp_path: Path) -> None:
    # No consent record ties a dataset to a subject any more: the subject is pinned in code.
    fixture = _corpus(tmp_path, subject_id="someone-else")
    with pytest.raises(SecurityError, match="not 'cole-self'"):
        _manifest(fixture)
    built = _manifest(_corpus(tmp_path / "cole"))
    foreign = built.model_copy(update={"subject_id": "someone-else"})
    with pytest.raises(SecurityError, match="not 'cole-self'"):
        validate_dataset_sources(fixture.layout, foreign)


def test_a_dataset_built_under_a_consent_record_still_validates_byte_for_byte(
    tmp_path: Path,
) -> None:
    fixture = _corpus(tmp_path)
    published = publish_dataset_manifest(fixture.layout, _manifest(fixture))
    manifest_path = published.directory / "manifest.json"
    stored = json.loads(manifest_path.read_bytes())
    stored["consent"] = {
        "consent_id": "cole-self-v1",
        "purpose": "private-fine-tuning",
        "file": {
            "path": "provenance/consents/cole-self-v1.json",
            "sha256": "c" * 64,
            "size_bytes": 1,
        },
    }
    data = json.dumps(stored, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    os.chmod(published.directory, 0o700)
    os.chmod(manifest_path, 0o600)
    manifest_path.write_bytes(data + b"\n")
    os.chmod(manifest_path, 0o400)
    os.chmod(published.directory, 0o500)
    revalidated = validate_published_dataset(fixture.layout, published.directory)
    assert revalidated.manifest.consent is not None
    assert not (fixture.layout.root / "provenance/consents").exists()


def test_transfer_plan_is_prohibited_by_default_and_payload_is_exact(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    published_dataset = publish_dataset_manifest(fixture.layout, _manifest(fixture))
    plan = build_transfer_plan(
        fixture.layout,
        plan_id="fine-tune-transfer-v1",
        dataset_id=published_dataset.manifest.dataset_id,
        created_by="cole",
        clock=lambda: NOW + timedelta(seconds=3),
    )
    published_plan = publish_transfer_plan(fixture.layout, plan)

    assert plan.hosted_transfer_status == "prohibited"
    assert plan.destination is None
    assert plan.approval is None
    assert plan.execution_performed is False
    assert {item.media_type for item in plan.payload} == {"application/json", "audio/wav"}
    assert len(plan.payload) == 4
    assert published_plan.plan == plan
    assert not hasattr(plan, "execute")


def test_hosted_transfer_requires_complete_exact_unexpired_approval(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    published_dataset = publish_dataset_manifest(fixture.layout, _manifest(fixture))
    prohibited = build_transfer_plan(
        fixture.layout,
        plan_id="prohibited-plan",
        dataset_id=published_dataset.manifest.dataset_id,
        created_by="cole",
        clock=lambda: NOW + timedelta(seconds=3),
    )
    with pytest.raises(SecurityError, match="requires destination"):
        build_transfer_plan(
            fixture.layout,
            plan_id="partial-approval",
            dataset_id=published_dataset.manifest.dataset_id,
            created_by="cole",
            destination="gpu.example.invalid/account/job",
            clock=lambda: NOW + timedelta(seconds=3),
        )

    approval_directory = fixture.layout.directory("provenance") / "transfer-approvals"
    approval_directory.mkdir(mode=0o700, exist_ok=True)
    approval_file = approval_directory / "hosted-v1.json"
    approval = HostedTransferApproval(
        approval_id="hosted-v1",
        approved=True,
        destination="gpu.example.invalid/account/job",
        dataset_manifest_sha256=published_dataset.manifest_sha256,
        payload_set_sha256=prohibited.payload_set_sha256,
        approved_at=NOW + timedelta(seconds=2),
        expires_at=NOW + timedelta(hours=1),
        approved_by="cole",
        statement="Approve this exact encrypted payload for this destination only.",
    )
    approval_sha256 = write_canonical_json(approval_file, approval)
    relative = approval_file.relative_to(fixture.layout.root).as_posix()
    approved = build_transfer_plan(
        fixture.layout,
        plan_id="approved-plan",
        dataset_id=published_dataset.manifest.dataset_id,
        created_by="cole",
        destination=approval.destination,
        approval_path=relative,
        approval_sha256=approval_sha256,
        clock=lambda: NOW + timedelta(seconds=3),
    )
    assert approved.hosted_transfer_status == "approved-not-executed"
    assert approved.approval is not None
    assert approved.execution_performed is False

    expired = approval.model_copy(update={"expires_at": NOW + timedelta(seconds=2, microseconds=1)})
    expired_sha256 = write_canonical_json(approval_file, expired)
    with pytest.raises(SecurityError, match="not currently effective"):
        build_transfer_plan(
            fixture.layout,
            plan_id="expired-plan",
            dataset_id=published_dataset.manifest.dataset_id,
            created_by="cole",
            destination=expired.destination,
            approval_path=relative,
            approval_sha256=expired_sha256,
            clock=lambda: NOW + timedelta(seconds=3),
        )


def test_hosted_transfer_rejects_payload_or_destination_mismatch(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    published = publish_dataset_manifest(fixture.layout, _manifest(fixture))
    prohibited = build_transfer_plan(
        fixture.layout,
        plan_id="base-plan",
        dataset_id=published.manifest.dataset_id,
        created_by="cole",
        clock=lambda: NOW + timedelta(seconds=3),
    )
    directory = fixture.layout.directory("provenance") / "transfer-approvals"
    directory.mkdir(mode=0o700, exist_ok=True)
    path = directory / "wrong.json"
    approval = HostedTransferApproval(
        approval_id="wrong",
        approved=True,
        destination="approved.example.invalid/job",
        dataset_manifest_sha256=published.manifest_sha256,
        payload_set_sha256="f" * 64,
        approved_at=NOW + timedelta(seconds=2),
        expires_at=NOW + timedelta(hours=1),
        approved_by="cole",
        statement="Intentionally mismatched test evidence.",
    )
    digest = write_canonical_json(path, approval)
    relative = path.relative_to(fixture.layout.root).as_posix()
    with pytest.raises(SecurityError, match="destination"):
        build_transfer_plan(
            fixture.layout,
            plan_id="wrong-destination",
            dataset_id=published.manifest.dataset_id,
            created_by="cole",
            destination="other.example.invalid/job",
            approval_path=relative,
            approval_sha256=digest,
            clock=lambda: NOW + timedelta(seconds=3),
        )
    with pytest.raises(SecurityError, match="payload"):
        build_transfer_plan(
            fixture.layout,
            plan_id="wrong-payload",
            dataset_id=published.manifest.dataset_id,
            created_by="cole",
            destination=approval.destination,
            approval_path=relative,
            approval_sha256=digest,
            clock=lambda: NOW + timedelta(seconds=3),
        )
    assert prohibited.payload_set_sha256 != approval.payload_set_sha256


def test_transfer_contract_cannot_claim_execution_or_mutate_payload_digest(
    tmp_path: Path,
) -> None:
    fixture = _corpus(tmp_path)
    published = publish_dataset_manifest(fixture.layout, _manifest(fixture))
    plan = build_transfer_plan(
        fixture.layout,
        plan_id="strict-plan",
        dataset_id=published.manifest.dataset_id,
        created_by="cole",
    )
    data = plan.model_dump(mode="python")
    data["execution_performed"] = True
    with pytest.raises(ValidationError):
        FineTuningTransferPlan.model_validate(data)
    data = plan.model_dump(mode="python")
    data["payload_set_sha256"] = "0" * 64
    with pytest.raises(ValidationError, match="payload-set digest"):
        FineTuningTransferPlan.model_validate(data)


def test_dataset_entry_contract_validation_matrix(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    manifest = _manifest(fixture)
    train = manifest.entries[0].model_dump(mode="python")
    holdout = manifest.entries[2].model_dump(mode="python")
    cases: list[tuple[dict[str, Any], str]] = []

    wrong_suite = deepcopy(train)
    wrong_suite["prompt_suite"]["suite_id"] = "different-suite"
    cases.append((wrong_suite, "suite does not match"))

    wrong_suite_digest = deepcopy(train)
    wrong_suite_digest["prompt_suite_sha256"] = "0" * 64
    cases.append((wrong_suite_digest, "digest does not match"))

    wrong_purpose = deepcopy(train)
    wrong_purpose["prompt_suite_purpose"] = "capture-pilot"
    cases.append((wrong_purpose, "purpose is inconsistent"))

    wrong_embedded_prompt = deepcopy(train)
    wrong_embedded_prompt["prompt"]["style"] = "Different style."
    cases.append((wrong_embedded_prompt, "exact embedded"))

    wrong_transcript = deepcopy(train)
    wrong_transcript["source"]["transcript"] = "Different transcript."
    cases.append((wrong_transcript, "transcript must exactly match"))

    wrong_review = deepcopy(train)
    wrong_review["headphone_review"]["record"]["source_sha256"] = "f" * 64
    cases.append((wrong_review, "review does not bind"))

    holdout_from_train = deepcopy(train)
    holdout_from_train["split"] = "permanent-holdout"
    cases.append((holdout_from_train, "requires held-out"))

    room_tone = deepcopy(train)
    room_tone["prompt"]["category"] = "room-tone"
    room_tone["prompt_suite"]["prompts"][1]["category"] = "room-tone"
    room_tone["prompt_suite_sha256"] = canonical_sha256(
        PromptSuite.model_validate(room_tone["prompt_suite"])
    )
    cases.append((room_tone, "Room tone|room tone"))

    nonholdout = deepcopy(holdout)
    nonholdout["split"] = "train"
    cases.append((nonholdout, "Held-out prompts|held-out prompts"))

    for data, message in cases:
        with pytest.raises(ValidationError, match=message):
            DatasetEntry.model_validate(data)


def test_dataset_manifest_contract_validation_matrix(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    base = _manifest(fixture).model_dump(mode="python")
    cases: list[tuple[dict[str, Any], str]] = []

    wrong_consent = deepcopy(base)
    wrong_consent["consent"] = {
        "consent_id": "cole-self-v1",
        "purpose": "private-zero-shot-evaluation",
        "file": {
            "path": "provenance/consents/cole-self-v1.json",
            "sha256": "c" * 64,
            "size_bytes": 1,
        },
    }
    cases.append((wrong_consent, "authorize private fine-tuning"))

    missing_gate = deepcopy(base)
    missing_gate["pilot_gate"] = None
    cases.append((missing_gate, "require.*pilot gate"))

    future_gate = deepcopy(base)
    future_gate["pilot_gate"]["record"]["approved_at"] = NOW + timedelta(hours=1)
    cases.append((future_gate, "cannot predate.*pilot-gate"))

    wrong_subject = deepcopy(base)
    wrong_subject["subject_id"] = "other-subject"
    cases.append((wrong_subject, "match the dataset's subject"))

    future_review = deepcopy(base)
    future_review["entries"][0]["headphone_review"]["record"]["reviewed_at"] = NOW + timedelta(
        hours=1
    )
    cases.append((future_review, "cannot predate headphone review"))

    missing_split = deepcopy(base)
    missing_split["entries"][1]["split"] = "train"
    cases.append((missing_split, "must all be non-empty"))

    duplicate_source = deepcopy(base)
    duplicate_source["entries"] = list(duplicate_source["entries"])
    duplicate_source["entries"][1] = deepcopy(duplicate_source["entries"][0])
    duplicate_source["entries"][1]["split"] = "validation"
    duplicate_source["entries"] = tuple(duplicate_source["entries"])
    cases.append((duplicate_source, "duplicate accepted utterance"))

    duplicate_accounting = deepcopy(base)
    duplicate_accounting["category_accounting"][1]["category"] = duplicate_accounting[
        "category_accounting"
    ][0]["category"]
    cases.append((duplicate_accounting, "rows must be unique"))

    missing_category = deepcopy(base)
    missing_category["category_accounting"] = missing_category["category_accounting"][:-1]
    cases.append((missing_category, "cover every realized category"))

    wrong_count = deepcopy(base)
    neutral_row = next(
        row for row in wrong_count["category_accounting"] if row["category"] == "neutral"
    )
    neutral_row["train_count"] = 0
    neutral_row["validation_count"] = 1
    cases.append((wrong_count, "accounting is wrong"))

    wrong_holdout_count = deepcopy(base)
    wrong_holdout_count["holdout_gate"]["utterance_count"] += 1
    cases.append((wrong_holdout_count, "utterance count"))

    wrong_holdout_duration = deepcopy(base)
    wrong_holdout_duration["holdout_gate"]["actual_duration_seconds"] += 1.0
    cases.append((wrong_holdout_duration, "duration does not match"))

    for data, message in cases:
        with pytest.raises(ValidationError, match=message):
            DatasetManifest.model_validate(data)

    with pytest.raises(ValidationError, match="timestamps require"):
        DatasetManifest.model_validate({**base, "created_at": datetime(2026, 8, 17)})


def test_small_gate_and_accounting_contracts_fail_closed() -> None:
    with pytest.raises(ValidationError, match="must be unique"):
        PilotGateApproval(
            gate_id="duplicate-gate",
            pilot_manifest_sha256s=("a" * 64, "a" * 64),
            approved=True,
            headphone_review_completed=True,
            full_corpus_capture_allowed=True,
            approved_at=NOW,
            approved_by="cole",
            statement="Duplicate evidence is invalid.",
        )
    with pytest.raises(ValidationError, match="sum to total"):
        CategoryAccounting(
            category="neutral",
            train_count=1,
            validation_count=0,
            permanent_holdout_count=0,
            total_count=2,
        )
    with pytest.raises(ValidationError, match="below the required"):
        HoldoutGate(
            minimum_duration_seconds=600.0,
            actual_duration_seconds=599.0,
            utterance_count=1,
        )
    with pytest.raises(ValidationError, match="expiry must follow"):
        HostedTransferApproval(
            approval_id="bad-expiry",
            approved=True,
            destination="gpu.example.invalid/job",
            dataset_manifest_sha256="a" * 64,
            payload_set_sha256="b" * 64,
            approved_at=NOW,
            expires_at=NOW,
            approved_by="cole",
            statement="Invalid expiry.",
        )


def _rebind_transfer_payload(data: dict[str, Any]) -> None:
    data["payload_set_sha256"] = canonical_sha256(data["payload"])


def test_transfer_plan_contract_validation_matrix(tmp_path: Path) -> None:
    fixture = _corpus(tmp_path)
    published = publish_dataset_manifest(fixture.layout, _manifest(fixture))
    plan = build_transfer_plan(
        fixture.layout,
        plan_id="contract-matrix",
        dataset_id=published.manifest.dataset_id,
        created_by="cole",
        clock=lambda: NOW + timedelta(seconds=3),
    )
    base = plan.model_dump(mode="python")
    cases: list[tuple[dict[str, Any], str]] = []

    duplicate = deepcopy(base)
    duplicate["payload"][1]["path"] = duplicate["payload"][0]["path"]
    _rebind_transfer_payload(duplicate)
    cases.append((duplicate, "paths must be unique"))

    missing_dataset = deepcopy(base)
    missing_dataset["payload"] = tuple(
        item
        for item in missing_dataset["payload"]
        if item["path"] != missing_dataset["dataset_manifest"]["path"]
    )
    _rebind_transfer_payload(missing_dataset)
    cases.append((missing_dataset, "include its exact dataset"))

    wrong_dataset_member = deepcopy(base)
    dataset_item = next(
        item
        for item in wrong_dataset_member["payload"]
        if item["path"] == wrong_dataset_member["dataset_manifest"]["path"]
    )
    dataset_item["sha256"] = "f" * 64
    _rebind_transfer_payload(wrong_dataset_member)
    cases.append((wrong_dataset_member, "does not match dataset binding"))

    prohibited_destination = deepcopy(base)
    prohibited_destination["destination"] = "gpu.example.invalid/job"
    cases.append((prohibited_destination, "prohibited hosted transfer"))

    missing_approval = deepcopy(base)
    missing_approval["hosted_transfer_status"] = "approved-not-executed"
    cases.append((missing_approval, "requires separate destination-bound approval"))

    for data, message in cases:
        with pytest.raises(ValidationError, match=message):
            FineTuningTransferPlan.model_validate(data)


def test_approved_transfer_contract_revalidation_and_idempotent_publish(
    tmp_path: Path,
) -> None:
    fixture = _corpus(tmp_path)
    published_dataset = publish_dataset_manifest(fixture.layout, _manifest(fixture))
    prohibited = build_transfer_plan(
        fixture.layout,
        plan_id="approval-basis",
        dataset_id=published_dataset.manifest.dataset_id,
        created_by="cole",
        clock=lambda: NOW + timedelta(seconds=3),
    )
    approval = HostedTransferApproval(
        approval_id="matrix-approval",
        approved=True,
        destination="gpu.example.invalid/exact-job",
        dataset_manifest_sha256=published_dataset.manifest_sha256,
        payload_set_sha256=prohibited.payload_set_sha256,
        approved_at=NOW + timedelta(seconds=2),
        expires_at=NOW + timedelta(days=1),
        approved_by="cole",
        statement="Approve only this exact payload and destination.",
    )
    approval_directory = fixture.layout.directory("provenance") / "transfer-approvals"
    approval_directory.mkdir(mode=0o700, exist_ok=True)
    approval_file = approval_directory / "matrix-approval.json"
    approval_sha256 = write_canonical_json(approval_file, approval)
    plan = build_transfer_plan(
        fixture.layout,
        plan_id="approved-matrix-plan",
        dataset_id=published_dataset.manifest.dataset_id,
        created_by="cole",
        destination=approval.destination,
        approval_path=approval_file.relative_to(fixture.layout.root).as_posix(),
        approval_sha256=approval_sha256,
        clock=lambda: NOW + timedelta(seconds=3),
    )

    validate_transfer_plan(fixture.layout, plan, at=NOW + timedelta(seconds=3))
    with pytest.raises(SecurityError, match="not currently effective"):
        validate_transfer_plan(fixture.layout, plan, at=NOW + timedelta(days=2))

    base = plan.model_dump(mode="python")
    cases: list[tuple[dict[str, Any], str]] = []
    wrong_destination = deepcopy(base)
    wrong_destination["destination"] = "gpu.example.invalid/other-job"
    cases.append((wrong_destination, "destination does not match"))
    wrong_payload = deepcopy(base)
    wrong_payload["approval"]["record"]["payload_set_sha256"] = "f" * 64
    cases.append((wrong_payload, "payload does not match"))
    wrong_dataset = deepcopy(base)
    wrong_dataset["approval"]["record"]["dataset_manifest_sha256"] = "f" * 64
    cases.append((wrong_dataset, "dataset manifest does not match"))
    future_approval = deepcopy(base)
    future_approval["approval"]["record"]["approved_at"] = NOW + timedelta(minutes=1)
    cases.append((future_approval, "not effective"))
    for data, message in cases:
        with pytest.raises(ValidationError, match=message):
            FineTuningTransferPlan.model_validate(data)

    first = publish_transfer_plan(
        fixture.layout,
        plan,
        at=NOW + timedelta(seconds=3),
    )
    second = publish_transfer_plan(
        fixture.layout,
        plan,
        at=NOW + timedelta(seconds=3),
    )
    assert second == first
    with pytest.raises(SecurityError, match="collision"):
        publish_transfer_plan(
            fixture.layout,
            plan.model_copy(update={"created_by": "someone-else"}),
            at=NOW + timedelta(seconds=3),
        )
