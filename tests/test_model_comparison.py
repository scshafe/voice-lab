"""Comparing model families: training summaries, the benchmark, records and the CLI
(model_comparison.py, cli_models.py, D-049).

The backend is the fake of ``speech_fixtures`` (a generated tone per seed); the run is
``finetune_speech_runs``'s; nothing starts a model or reads a recording.
"""

from __future__ import annotations

import json
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from speech_fixtures import NOW, FakeBackend, Lab, approve_consent, build_lab, write_tone

from cole_voice_lab import cli_models
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.cli import main
from cole_voice_lab.finetune import FINETUNE_OUTPUT_ROOT, load_finetune_dataset
from cole_voice_lab.model_comparison import (
    BENCHMARK_TEXTS,
    ComparisonError,
    ModelComparison,
    benchmark_run,
    comparison_id,
    comparison_rows,
    read_comparisons,
    record_comparison,
    require_blind_review,
    training_summary,
)
from cole_voice_lab.speech.gpu import current_holder, slot_wanted
from cole_voice_lab.speech.release import BlindReviewRef, VoiceReleaseError, history


@pytest.fixture
def lab(tmp_path: Path) -> Lab:
    made = build_lab(tmp_path, consent=False, release=False)
    approve_consent(made.layout, purposes=("private-home-host-synthesis",))
    log = made.layout.root / FINETUNE_OUTPUT_ROOT / "finetune-a" / "train.log"
    log.write_text(
        "Epoch 120/120: 100%|█| 9/9 [00:50<00:00, 6.03s/update, loss=0.4, update=1080]\n"
    )
    return made


def benchmark(lab: Lab, backend: Any | None = None, **options: Any) -> Any:
    ticks = iter(float(tick) for tick in range(0, 10_000))
    return benchmark_run(
        lab.layout,
        "finetune-a",
        backend or FakeBackend(),
        consent_id="cole-self-v1",
        now=NOW,
        work_directory=lab.layout.root / "tmp" / ".model-benchmark-test",
        monotonic=lambda: next(ticks),
        **options,
    )


def test_a_training_summary_reads_the_runs_records_and_log(lab: Lab) -> None:
    summary = training_summary(lab.layout, "finetune-a", training_device="RX 6900 XT")
    assert (summary.model_family, summary.model_name) == ("f5-tts", "F5TTS_v1_Base")
    assert (summary.epochs, summary.updates, summary.wall_seconds) == (3, 1080, 600.0)
    assert summary.device == "RX 6900 XT" and summary.dataset_clips == 2
    assert summary.dataset_sample_rate_hz == 24_000 and summary.dataset_seconds == 0.2
    assert summary.dataset_sha256 == lab.run.dataset_sha256
    assert set(summary.runtime) >= {"f5_tts_version", "torch_version", "pretrained_sha256"}


def test_a_benchmark_renders_the_held_out_set_through_the_port(lab: Lab) -> None:
    backend = FakeBackend(peak=1_600_000_000)
    result = benchmark(lab, backend)
    assert result.fingerprint.model_family == "f5-tts" and not result.designated
    assert len({timing.text_index for timing in result.segments}) == len(BENCHMARK_TEXTS)
    # One tick per clock read: loading took 1 s, each segment 1 s, 0.1 s of audio each.
    assert (result.load_ms, result.first_segment_ms, result.time_to_first_audio_ms) == (
        1000,
        1000,
        2000,
    )
    assert result.real_time_factor == 10.0
    assert result.deterministic is True and result.peak_memory_bytes == 1_600_000_000
    assert backend.calls[-1] == backend.calls[0]  # the rerun of the first segment
    assert not (lab.layout.root / "tmp" / ".model-benchmark-test").exists()
    assert history(lab.layout) == []  # a preview, never a release


def test_a_benchmark_notices_a_nondeterministic_backend(lab: Lab) -> None:
    renders = iter(range(1, 1000))

    def unseeded(path: Path) -> None:
        write_tone(path, period=20 + next(renders) % 40)

    assert benchmark(lab, FakeBackend(write=unseeded)).deterministic is False


def test_a_benchmark_needs_its_consent_and_held_out_texts(tmp_path: Path, lab: Lab) -> None:
    speech_only = build_lab(tmp_path / "speech-only", release=False)  # assistant speech only
    with pytest.raises(VoiceReleaseError) as refused:
        benchmark(speech_only)
    assert refused.value.code == "not_designatable"
    trained_on = ("And is there a third? " + BENCHMARK_TEXTS[0],)
    benchmark(lab, texts=trained_on)  # not a training text: fine
    dataset, _ = load_finetune_dataset(lab.layout, f"{FINETUNE_OUTPUT_ROOT}/finetune-a")
    with pytest.raises(ComparisonError, match="held out"):
        benchmark(lab, texts=(dataset.clips[0].text.upper(),))


def test_comparisons_are_written_once_and_compared_side_by_side(lab: Lab) -> None:
    training = training_summary(lab.layout, "finetune-a")
    first = ModelComparison(
        comparison_id=comparison_id(NOW),
        model_family="f5-tts",
        run_id="finetune-a",
        created_at=NOW,
        training=training,
    )
    path = record_comparison(lab.layout, first)
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert path.read_bytes() == canonical_json_bytes(first) + b"\n"
    with pytest.raises(FileExistsError):
        record_comparison(lab.layout, first)
    review = BlindReviewRef(review_id="review-1", score_sheet_sha256="a" * 64)
    with pytest.raises(ComparisonError):
        require_blind_review(lab.layout, review)
    second = first.model_copy(
        update={
            "comparison_id": comparison_id(NOW + timedelta(minutes=1)),
            "created_at": NOW + timedelta(minutes=1),
            "inference": benchmark(lab),
            "quality": review,
        }
    )
    record_comparison(lab.layout, second)
    records = read_comparisons(lab.layout)
    assert len(records) == 2 and read_comparisons(lab.layout, ("other",)) == []
    rows = comparison_rows(records)
    assert len(rows) == 1 and rows[0]["blind_review"] == "review-1"
    assert rows[0]["real_time_factor"] == 10.0 and rows[0]["epochs"] == 3
    with pytest.raises(ValueError, match="its run's"):
        ModelComparison.model_validate({**first.model_dump(), "run_id": "other"})


def test_the_cli_records_and_compares(
    lab: Lab, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = str(lab.layout.root)
    base = ["families", "record", "--asset-root", root, "--run", "finetune-a"]
    assert main([*base, "--training-device", "RX 6900 XT"]) == 0
    written = json.loads(capsys.readouterr().out)
    assert written["inference"] is None and written["training"]["device"] == "RX 6900 XT"
    assert main([*base, "--benchmark"]) == 2
    assert "--config" in capsys.readouterr().err
    config = lab.root / "speech.json"
    config.write_bytes(canonical_json_bytes(lab.config))
    seen: list[Any] = []

    def fake_backend(configuration: Any) -> FakeBackend:
        made = FakeBackend()
        seen.append((configuration, made))
        return made

    monkeypatch.setattr(cli_models, "ReleaseBackend", fake_backend)
    slot = Path(lab.config.gpu_slot_root)
    argv = [*base, "--benchmark", "--config", str(config), "--consent-id", "cole-self-v1"]
    assert main(argv) == 0
    benchmarked = json.loads(capsys.readouterr().out)
    assert benchmarked["inference"]["deterministic"] is True
    assert seen[0][1].unloads == 1 and not slot_wanted(slot) and current_holder(slot) is None
    (lab.layout.root / "evaluations/blind-reviews/review-1").mkdir(parents=True)
    review = ["--blind-review", "review-1", "--score-sheet-sha256", "b" * 64]
    assert main([*base, *review]) == 0
    capsys.readouterr()
    assert main(["families", "compare", "--asset-root", root]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["run_id"] for row in rows] == ["finetune-a"]
    assert rows[0]["blind_review"] == "review-1" and rows[0]["real_time_factor"] is None
