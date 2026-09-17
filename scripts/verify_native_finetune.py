"""Independently verify the durable evidence behind the native ``finetune`` suite.

Every assertion is re-derived from durable evidence through the canonical Python modules.
The harness reports are read only to name what to look up (operation, run and take ids) and
for what only the app can observe (its status lines and controls), never as the answer to
what happened on disk, so a harness that reported the wrong result still fails here.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from native_finetune_fixture import (
    READING_PROMPT_IDS,
    REFERENCE_PROMPT_ID,
    SLOW_TRAINING_STEP_SECONDS,
    NativeFineTuneFixture,
)
from verify_native_recovery import fixture_pcm, read_pcm
from verify_native_workspace import SUBJECT

from cole_voice_lab.canonical import sha256_file, write_canonical_json
from cole_voice_lab.finetune import (
    FINETUNE_OUTPUT_ROOT,
    FineTuneCorpus,
    FineTuneDataset,
    FineTuneRun,
    finetune_run_summary,
    latest_finished_finetune_run,
)
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_finetune_jobs import (
    CANCELLED_BY_OPERATOR,
    FINETUNE_JOBS_ROOT,
    FineTuneJob,
    FineTuneJobHead,
    FineTuneJobIntent,
)
from cole_voice_lab.recording_takes import RecordingTakeStore, read_recording_evidence

NOT_SET_UP = "Fine-tuning is not set up: this app has no F5 runtime to train with"
PROCESS_EXIT_SECONDS = 30.0


def _processes_mentioning(*paths: Path) -> list[str]:
    listing = subprocess.run(
        ["/bin/ps", "-axww", "-o", "pid=,command="], check=True, capture_output=True, text=True
    ).stdout
    own = str(os.getpid())
    return [
        line.strip()
        for line in listing.splitlines()
        if any(str(path) in line for path in paths) and line.split(None, 1)[0] != own
    ]


def _require_no_fixture_processes(*paths: Path) -> None:
    """Wait for the harness's service to exit; then no process may name the fixture paths."""

    deadline = time.monotonic() + PROCESS_EXIT_SECONDS
    while (remaining := _processes_mentioning(*paths)) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not remaining, f"fixture processes outlived the suite: {remaining}"


def _require_saved_original(store: RecordingTakeStore, layout: AssetLayout, take_id: str) -> None:
    take = store.get_take(take_id)
    assert take is not None and take.seal is not None, f"{take_id} was not sealed"
    assert take.decision == "kept", f"{take_id} was not saved: {take.decision}"
    original = layout.root / take.seal.original_path
    assert sha256_file(original)[0] == take.seal.original_sha256, f"{take_id} original changed"
    frames, pcm = read_pcm(original)
    assert frames == 4801 and pcm == fixture_pcm(frames), f"{take_id} is not the fixture signal"


def verify(
    root: Path, *, trained: NativeFineTuneFixture, unavailable: NativeFineTuneFixture
) -> dict[str, object]:
    """Check both fixture roots; write ``root/python-finetune-evidence.json``.

    Returns: the evidence written.
    Raises: ``AssertionError`` naming the first disagreement.
    """

    report = json.loads((root / "native" / "finetune-report.json").read_text())
    layout = AssetLayout.validate(trained.layout.root)
    store = RecordingTakeStore(layout, read_only=True)
    jobs = layout.root / FINETUNE_JOBS_ROOT

    # The app always asks for the default length and lets unrecorded prompts be missing.
    finished_op = report["finished"]["operation_id"]
    cancelled_op = report["cancelled"]["operation_id"]
    assert finished_op != cancelled_op
    for operation in (finished_op, cancelled_op):
        intent = read_recording_evidence(jobs / "requests" / f"{operation}.json", FineTuneJobIntent)
        assert intent.subject_id == SUBJECT
        assert intent.command.allow_missing is True and intent.command.epochs is None
    assert sorted(path.stem for path in (jobs / "requests").iterdir()) == sorted(
        [finished_op, cancelled_op]
    ), "the app submitted a job the suite did not start"
    assert read_recording_evidence(jobs / "head.json", FineTuneJobHead).job is None
    finished = read_recording_evidence(jobs / "terminal" / f"{finished_op}.json", FineTuneJob)
    cancelled = read_recording_evidence(jobs / "terminal" / f"{cancelled_op}.json", FineTuneJob)
    assert finished.state == "finished" and finished.error is None
    assert cancelled.state == "cancelled" and cancelled.error is None
    assert finished.run_id == report["finished"]["run_id"]
    assert cancelled.run_id == report["cancelled"]["run_id"]
    assert finished.run_root is not None and cancelled.run_root is not None
    assert finished.finished_at is not None and cancelled.started_at is not None
    assert finished.finished_at <= cancelled.started_at

    # The finished run: the newest finished run, trained on the takes recorded before it.
    latest = latest_finished_finetune_run(layout)
    assert latest is not None and latest.run_id == finished.run_id
    summary = finetune_run_summary(layout, finished.run_root)
    assert summary.run_root == f"{FINETUNE_OUTPUT_ROOT}/{finished.run_id}"
    assert summary.status == "finished" and summary.prompt_count == 2
    run = FineTuneRun.model_validate_json(
        (layout.root / finished.run_root / "run.json").read_bytes()
    )
    assert run.status == "finished" and run.subject_id == SUBJECT and run.error is None
    assert summary.finetuned_sample_paths and summary.base_sample_paths
    for sample in (*summary.finetuned_sample_paths, *summary.base_sample_paths):
        assert (layout.root / sample).is_file(), f"missing sample {sample}"
    dataset_root = layout.root / finished.run_root / "dataset"
    corpus = FineTuneCorpus.model_validate_json((dataset_root / "corpus.json").read_bytes())
    dataset = FineTuneDataset.model_validate_json((dataset_root / "dataset.json").read_bytes())
    assert corpus.subject_id == SUBJECT and corpus.catalog_reference == trained.reference
    assert [entry.prompt_id for entry in corpus.entries] == [
        REFERENCE_PROMPT_ID,
        READING_PROMPT_IDS[0],
    ]
    assert corpus.entries[0].prompt.reference_candidate is True
    assert list(corpus.missing_prompt_ids) == list(READING_PROMPT_IDS[1:])
    corpus_takes = [entry.take_id for entry in corpus.entries]
    assert corpus_takes == report["corpus_take_ids"]
    for entry in corpus.entries:
        _require_saved_original(store, layout, entry.take_id)
        take = store.get_take(entry.take_id)
        assert take is not None and take.seal is not None
        assert entry.source_sha256 == take.seal.original_sha256
    assert [clip.prompt_id for clip in dataset.clips] == [
        entry.prompt_id for entry in corpus.entries
    ]
    for clip in dataset.clips:
        assert sha256_file(dataset_root / clip.relative_path)[0] == clip.sha256
    checkpoints = trained.runtime_root / "lib/python3.12/ckpts"
    assert (checkpoints / finished.run_id).is_symlink(), "a finished run keeps its runtime link"

    # The cancelled run: stopped mid-training, recorded as cancelled, its link released.
    stopped = FineTuneRun.model_validate_json(
        (layout.root / cancelled.run_root / "run.json").read_bytes()
    )
    assert stopped.status == "failed" and stopped.error == CANCELLED_BY_OPERATOR
    log = (layout.root / cancelled.run_root / "train.log").read_text(errors="replace")
    assert "Epoch " in log, "the cancelled job never trained"
    assert "Saved last checkpoint" not in log, "the cancelled job finished training"
    assert not os.path.lexists(checkpoints / cancelled.run_id), "cancel left the runtime link"
    assert not os.path.lexists(layout.root / cancelled.run_root / "samples")
    stub_mode = json.loads((trained.runtime_root / "stub-mode.json").read_text())
    assert stub_mode == {"step_seconds": SLOW_TRAINING_STEP_SECONDS}, stub_mode

    # A take recorded and saved while the second job was training.
    during = report["during_job_take_id"]
    assert during not in corpus_takes
    _require_saved_original(store, layout, during)
    take = store.get_take(during)
    assert take is not None and take.seal is not None
    assert take.reservation.prompt.prompt_id == READING_PROMPT_IDS[1]
    assert cancelled.finished_at is not None
    assert cancelled.started_at <= take.seal.first_sample_at <= cancelled.finished_at, (
        "the take was not recorded while the job ran"
    )
    during_job = report["during_job"]
    assert during_job["running_before_record"] is True
    assert during_job["running_after_save"] is True
    assert during_job["navigation_available"] is True and during_job["save_available"] is True

    # What the app showed.
    assert report["runtime_configured"] is True
    assert report["finished"]["status_region_line"] == f"Model ready · {finished.run_id}"
    assert report["cancelled"]["status_region_line"] == "Fine-tune cancelled"
    assert any(line.startswith("Fine-tuning · pass ") for line in report["finished"]["lines"])
    assert any(line.startswith("Fine-tuning · pass ") for line in report["cancelled"]["lines"])
    assert "training" in report["finished"]["stages"]
    assert report["slots"] == [
        "transport.fine-tune",
        "transport.cancel-fine-tune",
        "transport.fine-tune",
        "transport.cancel-fine-tune",
        "transport.fine-tune",
    ]
    assert report["fixed_geometry"] is True
    assert report["microphone_opened"] is False and report["window_shown"] is False

    # The unavailable root: the control explained itself and nothing was submitted.
    missing = json.loads(
        (root / "native-unavailable" / "finetune-unavailable-report.json").read_text()
    )
    unavailable_layout = AssetLayout.validate(unavailable.layout.root)
    assert not (unavailable.runtime_root / "bin" / "python").exists()
    assert missing["runtime_configured"] is False
    assert missing["slot"] == {
        "title": "Fine-tune model",
        "identifier": "transport.fine-tune",
        "enabled": False,
        "help": NOT_SET_UP,
    }
    assert missing["status_region_source"] == "recording"
    assert not any((unavailable_layout.root / FINETUNE_JOBS_ROOT / "requests").iterdir())
    assert not os.path.lexists(unavailable_layout.root / FINETUNE_OUTPUT_ROOT)

    _require_no_fixture_processes(
        layout.root, trained.runtime_root, unavailable_layout.root, unavailable.runtime_root
    )
    evidence: dict[str, object] = {
        "subject_id": SUBJECT,
        "finished_run_id": finished.run_id,
        "finished_prompt_ids": [entry.prompt_id for entry in corpus.entries],
        "missing_prompt_ids": list(corpus.missing_prompt_ids),
        "finished_samples": len(summary.finetuned_sample_paths) + len(summary.base_sample_paths),
        "cancelled_run_id": cancelled.run_id,
        "cancelled_run_error": stopped.error,
        "cancelled_runtime_link_released": True,
        "take_saved_during_job": during,
        "unavailable_reason": NOT_SET_UP,
        "fixture_processes_remaining": 0,
    }
    write_canonical_json(root / "python-finetune-evidence.json", evidence)
    return evidence
