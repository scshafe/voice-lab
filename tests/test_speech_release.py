"""The voice release: designation, its chain, and the checks before every render (D-045).

No consent record is read (D-050): designation needs none, and only the pinned subject's run
can be designated.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from speech_fixtures import NOW, build_lab, finished_run, forge_release

from cole_voice_lab.canonical import sha256_file
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.finetune import FineTuneDigestCache
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.speech.release import (
    BlindReviewRef,
    VoiceRelease,
    VoiceReleaseError,
    current_release,
    designate,
    history,
    record_bytes,
    require_subject,
    verify_release,
)


def refused(code: str, call: object) -> VoiceReleaseError:
    with pytest.raises(VoiceReleaseError) as failure:
        call()  # type: ignore[operator]
    assert failure.value.code == code, failure.value
    return failure.value


def test_a_designation_binds_the_run_and_its_files_and_reads_no_consent(tmp_path: Path) -> None:
    lab = build_lab(tmp_path, release=False)
    # No consent record exists, and an unreadable one where they used to be is never read.
    consents = lab.layout.root / "provenance/consents"
    consents.mkdir(mode=0o700)
    (consents / "cole-self-v1.json").write_bytes(b"not a consent record")
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
    assert (
        release.file("checkpoint").path == "derived/finetune/finetune-a/checkpoints/model_last.pt"
    )
    assert release.file("checkpoint").sha256 == lab.run.checkpoints[0].sha256
    assert release.model.reference_prompt_id == "reference-warm-01"
    assert (
        release.file("reference").sha256
        == sha256_file(lab.layout.root / release.file("reference").path)[0]
    )
    assert (release.consent_id, release.consent_sha256) == (None, None)
    assert b"consent" not in record_bytes(release)
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
    assert second.model.reference_prompt_id == "neutral-01"
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
        "model_family": "f5-tts",
        "run_id": "r",
        "run_record_sha256": "a" * 64,
        "files": [{"role": "checkpoint", "path": "c", "sha256": "a" * 64}],
        "model": {
            "family": "f5-tts",
            "reference_prompt_id": "p",
            "reference_text_sha256": "a" * 64,
            "runtime": {"f5_tts_version": "1", "torch_version": "2", "pretrained_sha256": "a" * 64},
        },
        "consent_id": "c",
        "consent_sha256": "a" * 64,
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


def test_only_a_finished_run_can_be_designated_and_no_consent_is_needed(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    finished_run(layout)
    # No consent record anywhere in the lab: a finished run of the pinned subject is enough.
    assert not (layout.root / "provenance/consents").exists()
    release, _ = designate(layout, run_id="finetune-a", designated_by="Cole", now=NOW)
    assert release.subject_id == "cole-self" and release.consent_id is None
    refused(
        "not_designatable",
        lambda: designate(layout, run_id="nowhere", designated_by="Cole", now=NOW),
    )
    finished_run(layout, "finetune-running", status="running", finished_at=None)
    refused(
        "not_designatable",
        lambda: designate(layout, run_id="finetune-running", designated_by="Cole", now=NOW),
    )
    assert len(history(layout)) == 1


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
    checkpoint = lab.layout.root / release.file("checkpoint").path
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
    assert verified.family_state.reference.prompt_id == release.model.reference_prompt_id  # type: ignore[attr-defined]
    assert set(verified.files) == {"checkpoint", "reference"} and verified.max_segment_bytes > 50
    root = lab.layout.root
    if what == "checkpoint":
        (root / release.file("checkpoint").path).write_bytes(b"retrained")
    elif what == "reference":
        reference = root / release.file("reference").path
        reference.write_bytes(reference.read_bytes()[:-2] + b"\x01\x02")
    else:
        run = root / "derived/finetune/finetune-a/run.json"
        run.write_text(run.read_text().replace('"finished"', '"failed"'))
    refused(
        "release_changed",
        lambda: verify_release(lab.layout, release, digest, digests=FineTuneDigestCache()),
    )


def test_a_release_written_before_d050_keeps_its_consent_binding_byte_for_byte(
    tmp_path: Path,
) -> None:
    # A release designated under a consent record (D-045 to D-049) still validates and keeps
    # its exact bytes; nothing reads the record it names, so verification needs none.
    lab = build_lab(tmp_path, release=False)
    new, _ = designate(lab.layout, run_id="finetune-a", designated_by="Cole", now=NOW, write=False)
    stored = json.loads(record_bytes(new))
    stored |= {"consent_id": "cole-self-v1", "consent_sha256": "c" * 64}
    data = json.dumps(stored, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    old = VoiceRelease.model_validate_json(data)
    assert (old.consent_id, old.consent_sha256) == ("cole-self-v1", "c" * 64)
    assert record_bytes(old) == data
    # (The preview made the directory; it wrote nothing.)
    (lab.layout.root / "provenance/voice-releases/0001.json").write_bytes(data)
    release, digest = current_release(lab.layout)
    assert release == old
    verify_release(lab.layout, release, digest, digests=FineTuneDigestCache())
    require_subject(release, "cole-self")
    # A new designation chains onto it and names no consent record.
    second, _ = designate(lab.layout, run_id="finetune-a", designated_by="Cole", now=NOW)
    assert second.previous_release_sha256 == digest and second.consent_id is None


@pytest.mark.parametrize(
    "fields",
    [
        {"consent_id": None, "consent_sha256": None},
        {"consent_id": "cole-self-v1"},
        {"consent_sha256": "c" * 64},
    ],
)
def test_a_stored_consent_binding_is_whole_or_absent_never_null(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    lab = build_lab(tmp_path, release=False)
    new, _ = designate(lab.layout, run_id="finetune-a", designated_by="Cole", now=NOW, write=False)
    with pytest.raises(ValueError):
        VoiceRelease.model_validate({**new.model_dump(mode="json"), **fields})


def test_a_release_file_that_is_not_a_release_is_refused(tmp_path: Path) -> None:
    lab = build_lab(tmp_path)
    first = lab.layout.root / "provenance/voice-releases/0001.json"
    first.chmod(0o600)
    first.write_text("{}")
    with pytest.raises(ContractError, match="invalid voice release"):
        history(lab.layout)


def test_only_the_pinned_subjects_run_can_be_designated(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path / "lab")
    finished_run(layout, "finetune-other", subject_id="someone-else")
    # Another subject's run: never the voice, as a release or as a benchmark's preview.
    for write in (True, False):
        message = refused(
            "not_designatable",
            lambda write=write: designate(
                layout, run_id="finetune-other", designated_by="Cole", now=NOW, write=write
            ),
        )
        assert "pinned subject" in str(message)
    assert history(layout) == []
    # A release of another subject written into the chain by hand is refused at render.
    forged = forge_release(layout, "finetune-other")
    with pytest.raises(VoiceReleaseError, match="pinned subject") as failure:
        require_subject(forged, "cole-self")
    assert failure.value.code == "release_changed"


def test_a_preview_designates_nothing(tmp_path: Path) -> None:
    lab = build_lab(tmp_path, release=False)
    preview, _ = designate(
        lab.layout, run_id="finetune-a", designated_by="model comparison", now=NOW, write=False
    )
    assert preview.consent_id is None
    assert history(lab.layout) == []
    refused("no_release", lambda: current_release(lab.layout))
