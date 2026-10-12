"""The voice release: designation, its chain, and the checks before every render (D-045)."""

from __future__ import annotations

import json
import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from speech_fixtures import NOW, approve_consent, build_lab, finished_run

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.consent import revoke_consent
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.finetune import FineTuneDigestCache
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.speech.release import (
    BlindReviewRef,
    VoiceRelease,
    VoiceReleaseError,
    consent_path,
    current_release,
    designate,
    history,
    record_bytes,
    require_release_consent,
    verify_release,
)


def refused(code: str, call: object) -> VoiceReleaseError:
    with pytest.raises(VoiceReleaseError) as failure:
        call()  # type: ignore[operator]
    assert failure.value.code == code, failure.value
    return failure.value


def test_a_designation_binds_the_run_its_files_and_the_consent(tmp_path: Path) -> None:
    lab = build_lab(tmp_path, release=False)
    refused("no_release", lambda: current_release(lab.layout))
    preview, preview_digest = designate(
        lab.layout, run_id="finetune-a", designated_by="Cole", now=NOW, write=False
    )
    assert history(lab.layout) == []
    release, digest = designate(lab.layout, run_id="finetune-a", designated_by="Cole", now=NOW)
    assert (release, digest) == (preview, preview_digest)
    path = lab.layout.root / "provenance/voice-releases/0001.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert path.read_bytes() == record_bytes(release) and sha256_file(path)[0] == digest
    run_root = lab.layout.root / "derived/finetune/finetune-a"
    assert release.run_record_sha256 == sha256_file(run_root / "run.json")[0]
    assert release.checkpoint_path == "derived/finetune/finetune-a/checkpoints/model_last.pt"
    assert release.checkpoint_sha256 == lab.run.checkpoints[0].sha256
    assert release.reference_prompt_id == "reference-warm-01"
    assert release.reference_sha256 == sha256_file(lab.layout.root / release.reference_path)[0]
    assert (release.consent_id, release.consent_sha256) == ("cole-self-v1", lab.consent_sha256)
    assert (release.release_id, release.sequence, release.previous_release_sha256) == (
        "vr-0001",
        1,
        None,
    )
    assert current_release(lab.layout) == (release, digest)
    text = path.read_text()
    assert str(lab.layout.root) not in text


def test_rotation_appends_and_keeps_the_history(tmp_path: Path) -> None:
    lab = build_lab(tmp_path)
    finished_run(lab.layout, "finetune-b")
    first, first_digest = current_release(lab.layout)
    second, second_digest = designate(
        lab.layout,
        run_id="finetune-b",
        designated_by="Cole",
        now=NOW + timedelta(hours=1),
        reference_prompt_id="neutral-01",
    )
    assert (second.sequence, second.previous_release_sha256) == (2, first_digest)
    assert second.reference_prompt_id == "neutral-01"
    assert history(lab.layout) == [(first, first_digest), (second, second_digest)]
    assert current_release(lab.layout) == (second, second_digest)


@pytest.mark.parametrize("damage", ["gap", "stray", "chain", "canonical"])
def test_a_damaged_history_is_refused(tmp_path: Path, damage: str) -> None:
    lab = build_lab(tmp_path)
    directory = lab.layout.root / "provenance/voice-releases"
    first = directory / "0001.json"
    if damage == "gap":
        os.rename(first, directory / "0002.json")
        with pytest.raises(SecurityError, match="gap"):
            history(lab.layout)
    elif damage == "stray":
        (directory / "notes.txt").write_text("x")
        with pytest.raises(SecurityError, match="stray"):
            history(lab.layout)
    else:
        lab.designate()
        first.chmod(0o600)
        record = json.loads(first.read_text())
        if damage == "chain":
            record["designated_by"] = "Someone else"
            first.write_bytes(record_bytes(VoiceRelease.model_validate_json(json.dumps(record))))
            with pytest.raises(SecurityError, match="chain is broken"):
                history(lab.layout)
        else:
            first.write_text(json.dumps(record, indent=1))
            with pytest.raises(SecurityError, match="canonical"):
                history(lab.layout)


def test_a_release_record_is_strict() -> None:
    fields = {
        "release_id": "vr-0002",
        "sequence": 1,
        "previous_release_sha256": None,
        "subject_id": "cole-self",
        "run_id": "r",
        "run_record_sha256": "a" * 64,
        "checkpoint_path": "c",
        "checkpoint_sha256": "a" * 64,
        "reference_prompt_id": "p",
        "reference_path": "p",
        "reference_sha256": "a" * 64,
        "reference_text_sha256": "a" * 64,
        "consent_id": "c",
        "consent_sha256": "a" * 64,
        "runtime": {},
        "created_at": "2026-10-11T12:00:00Z",
        "designated_by": "Cole",
    }
    with pytest.raises(ValueError, match="four-digit sequence"):
        VoiceRelease.model_validate_json(json.dumps(fields))
    with pytest.raises(ValueError, match="only the first release"):
        VoiceRelease.model_validate_json(
            json.dumps({**fields, "release_id": "vr-0001", "previous_release_sha256": "b" * 64})
        )
    with pytest.raises(ValueError, match="who designated"):
        VoiceRelease.model_validate_json(
            json.dumps({**fields, "release_id": "vr-0001", "designated_by": "  "})
        )


def test_only_a_finished_run_of_the_consenting_subject_can_be_designated(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    finished_run(layout)
    message = refused(
        "not_designatable",
        lambda: designate(layout, run_id="finetune-a", designated_by="Cole", now=NOW),
    )
    assert "consent" in str(message)
    approve_consent(layout, purposes=("private-fine-tuning",))
    refused(
        "not_designatable",
        lambda: designate(layout, run_id="finetune-a", designated_by="Cole", now=NOW),
    )
    refused(
        "not_designatable",
        lambda: designate(layout, run_id="nowhere", designated_by="Cole", now=NOW),
    )
    finished_run(layout, "finetune-running", status="running", finished_at=None)
    refused(
        "not_designatable",
        lambda: designate(layout, run_id="finetune-running", designated_by="Cole", now=NOW),
    )


def test_a_designation_refuses_changed_files_and_unknown_reviews(tmp_path: Path) -> None:
    lab = build_lab(tmp_path, release=False)
    with pytest.raises(VoiceReleaseError, match="no blind review"):
        designate(
            lab.layout,
            run_id="finetune-a",
            designated_by="Cole",
            now=NOW,
            blind_review=BlindReviewRef(review_id="review-1", score_sheet_sha256="a" * 64),
        )
    (lab.layout.root / "evaluations/blind-reviews/review-1").mkdir(parents=True)
    release, _ = designate(
        lab.layout,
        run_id="finetune-a",
        designated_by="Cole",
        now=NOW,
        blind_review=BlindReviewRef(review_id="review-1", score_sheet_sha256="a" * 64),
    )
    assert release.blind_review is not None
    checkpoint = lab.layout.root / release.checkpoint_path
    checkpoint.write_bytes(b"other weights")
    refused(
        "not_designatable",
        lambda: designate(lab.layout, run_id="finetune-a", designated_by="Cole", now=NOW),
    )


@pytest.mark.parametrize("what", ["checkpoint", "reference", "run"])
def test_verification_refuses_a_release_whose_files_changed(tmp_path: Path, what: str) -> None:
    lab = build_lab(tmp_path)
    release, digest = current_release(lab.layout)
    verified = verify_release(lab.layout, release, digest, digests=FineTuneDigestCache())
    assert verified.resolution.reference.prompt_id == release.reference_prompt_id
    root = lab.layout.root
    if what == "checkpoint":
        (root / release.checkpoint_path).write_bytes(b"retrained")
    elif what == "reference":
        reference = root / release.reference_path
        reference.write_bytes(reference.read_bytes()[:-2] + b"\x01\x02")
    else:
        run = root / "derived/finetune/finetune-a/run.json"
        run.write_text(run.read_text().replace('"finished"', '"failed"'))
    refused(
        "release_changed",
        lambda: verify_release(lab.layout, release, digest, digests=FineTuneDigestCache()),
    )


def test_a_release_names_one_exact_consent_record(tmp_path: Path) -> None:
    lab = build_lab(tmp_path)
    release, _ = current_release(lab.layout)
    require_release_consent(lab.layout, release, at=NOW)
    refused(
        "consent_changed",
        lambda: require_release_consent(lab.layout, release, at=NOW + timedelta(days=400)),
    )
    path = consent_path(lab.layout, release.consent_id)
    revoke_consent(path, expected_sha256=release.consent_sha256, at=NOW)
    refused(
        "consent_changed",
        lambda: require_release_consent(lab.layout, release, at=NOW + timedelta(seconds=1)),
    )
    with pytest.raises(ValueError):
        consent_path(lab.layout, "../escape")


def test_a_release_file_that_is_not_a_release_is_refused(tmp_path: Path) -> None:
    lab = build_lab(tmp_path)
    first = lab.layout.root / "provenance/voice-releases/0001.json"
    first.chmod(0o600)
    first.write_text("{}")
    with pytest.raises(ContractError, match="invalid voice release"):
        history(lab.layout)
