"""Comparing model families: training summaries, the benchmark, records and the CLI
(model_comparison.py, cli_models.py, D-049).

The backend is the fake of ``speech_fixtures`` (a generated tone per seed); the run is
``finetune_speech_runs``'s; nothing starts a model or reads a recording. The host fingerprint
and the GPU's other users are fixed doubles; the blind review's package check
(``blind.aggregate_scores``, tested in test_blind.py) is replaced where a link is made.
"""

from __future__ import annotations

import hashlib
import json
import logging
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict
from speech_fixtures import NOW, FakeBackend, Lab, approve_consent, build_lab, write_tone

from cole_voice_lab import cli_models, model_comparison
from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.cli import main
from cole_voice_lab.finetune import FINETUNE_OUTPUT_ROOT, load_finetune_dataset
from cole_voice_lab.host_fingerprint import (
    Accelerator,
    GpuContention,
    HostFingerprint,
    record_training_host,
)
from cole_voice_lab.model_comparison import (
    BENCHMARK_TEXTS,
    ComparisonError,
    ModelComparison,
    QualityLink,
    benchmark_run,
    blind_review_link,
    comparison_id,
    comparison_rows,
    comparison_warnings,
    read_comparisons,
    record_comparison,
    remove_scratch,
    sweep_benchmark_scratch,
    training_summary,
)
from cole_voice_lab.speech.gpu import current_holder, slot_wanted
from cole_voice_lab.speech.release import VoiceReleaseError, history

HOST = HostFingerprint(
    system="Linux 7.2.8",
    machine="x86_64",
    cpu_model="Synthetic CPU 8000",
    cpu_count=16,
    memory_bytes=64 * 2**30,
    accelerators=(
        Accelerator(vendor_id="0x1002", device_id="0x73bf", driver="amdgpu", vram_bytes=2**34),
    ),
    rocm_version="7.0.0",
    mps=False,
    recorded_at=NOW,
)
QUIET = GpuContention(vram_used_bytes=2**30, vram_total_bytes=2**34, gpu_processes=())
BUSY = GpuContention(
    vram_used_bytes=9 * 2**30, vram_total_bytes=2**34, gpu_processes=("llama-server",)
)


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
    options.setdefault("now", NOW)
    options.setdefault("host", lambda: HOST)
    options.setdefault("contention", lambda: QUIET)
    return benchmark_run(
        lab.layout,
        "finetune-a",
        backend or FakeBackend(),
        consent_id="cole-self-v1",
        work_directory=lab.root / "scratch" / ".model-benchmark-test",
        monotonic=lambda: next(ticks),
        **options,
    )


def comparison(lab: Lab, minutes: int, **fields: Any) -> ModelComparison:
    when = NOW + timedelta(minutes=minutes)
    return ModelComparison(
        comparison_id=comparison_id(when),
        model_family="f5-tts",
        run_id="finetune-a",
        created_at=when,
        training=training_summary(lab.layout, "finetune-a"),
        **fields,
    )


def test_a_training_summary_reads_the_runs_records_log_and_host(lab: Lab) -> None:
    summary = training_summary(lab.layout, "finetune-a", training_device="RX 6900 XT")
    assert (summary.model_family, summary.model_name) == ("f5-tts", "F5TTS_v1_Base")
    assert (summary.epochs, summary.updates, summary.wall_seconds) == (3, 1080, 600.0)
    assert summary.device == "RX 6900 XT" and summary.dataset_clips == 2
    assert summary.dataset_sample_rate_hz == 24_000 and summary.dataset_seconds == 0.2
    assert summary.dataset_sha256 == lab.run.dataset_sha256
    assert summary.corpus_sha256 == lab.run.corpus_sha256
    assert set(summary.runtime) >= {"f5_tts_version", "torch_version", "pretrained_sha256"}
    assert summary.host is None  # trained before runs recorded their host
    record_training_host(lab.layout, f"{FINETUNE_OUTPUT_ROOT}/finetune-a", HOST)
    assert training_summary(lab.layout, "finetune-a").host == HOST


def test_a_benchmark_renders_the_held_out_set_through_the_port(lab: Lab) -> None:
    backend = FakeBackend(peak=1_600_000_000)
    seen = iter((QUIET, BUSY))
    loaded_at_snapshot: list[bool] = []

    def contention() -> GpuContention:
        loaded_at_snapshot.append(backend.loaded)
        return next(seen)

    result = benchmark(lab, backend, contention=contention, sandboxed=True)
    # Before the load and after the unload: the benchmark's own worker is never counted.
    assert loaded_at_snapshot == [False, False] and backend.unloads == 1
    assert result.fingerprint.model_family == "f5-tts" and not result.designated
    assert len({timing.text_index for timing in result.segments}) == len(BENCHMARK_TEXTS)
    # One tick per clock read: loading took 1 s, each segment 1 s, 0.1 s of audio each.
    assert (result.load_ms, result.first_segment_ms, result.time_to_first_audio_ms) == (
        1000,
        1000,
        2000,
    )
    assert result.real_time_factor == 10.0 and result.deterministic is True
    assert result.peak_memory is not None and result.peak_memory.bytes == 1_600_000_000
    assert result.peak_memory.method == "fake"  # how it was measured travels with it
    assert (result.host, result.contention_before, result.contention_after) == (HOST, QUIET, BUSY)
    assert result.sandboxed
    assert backend.calls[-1] == backend.calls[0]  # the rerun of the first segment
    assert not (lab.root / "scratch" / ".model-benchmark-test").exists()
    assert history(lab.layout) == []  # a preview, never a release


def test_a_rerun_of_a_benchmark_renders_with_the_same_seeds(lab: Lab) -> None:
    # The preview release is dated, so its digest changes; the seeds must not.
    first = benchmark(lab)
    again = benchmark(lab, now=NOW + timedelta(days=3))
    assert first.release_sha256 != again.release_sha256
    assert first.seed_basis_sha256 == again.seed_basis_sha256
    assert [item.sha256 for item in first.segments] == [item.sha256 for item in again.segments]


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


def test_scratch_that_cannot_be_removed_is_logged_and_leftovers_are_swept(
    lab: Lab, caplog: pytest.LogCaptureFixture
) -> None:
    scratch = lab.root / "state" / "speech" / "benchmark"
    stuck = scratch / ".model-benchmark-stuck"
    (stuck / "inner").mkdir(parents=True)
    (stuck / "inner" / "0001.wav").write_bytes(b"x")
    (stuck / "inner").chmod(0o500)
    try:
        with caplog.at_level(logging.WARNING, logger="cole_voice_lab.model_comparison"):
            remove_scratch(stuck)
        assert "could not remove" in caplog.text and stuck.exists()
    finally:
        (stuck / "inner").chmod(0o700)
    keep = scratch / ".model-benchmark-mine"
    keep.mkdir()
    (scratch / ".model-benchmark-killed").mkdir()
    (scratch / "other").mkdir()
    removed = sweep_benchmark_scratch(scratch, keep=keep)
    assert removed == (".model-benchmark-killed", ".model-benchmark-stuck")
    assert keep.exists() and (scratch / "other").exists() and not stuck.exists()
    remove_scratch(scratch / ".model-benchmark-absent")  # nothing there: nothing to do
    assert sweep_benchmark_scratch(lab.root / "absent") == ()


def test_compare_composes_each_runs_newest_training_benchmark_and_review(lab: Lab) -> None:
    first = comparison(lab, 0)
    path = record_comparison(lab.layout, first)
    assert stat.S_IMODE(path.stat().st_mode) == 0o400
    assert path.read_bytes() == canonical_json_bytes(first) + b"\n"
    with pytest.raises(FileExistsError):
        record_comparison(lab.layout, first)
    measured = comparison(lab, 1, inference=benchmark(lab))
    record_comparison(lab.layout, measured)
    review = QualityLink(
        review_id="review-1",
        score_sheet_sha256="a" * 64,
        candidate_id="finetune-a",
        sample_count=6,
        overall_mean=4.2,
        means={"likeness": 4.2},
    )
    judged = comparison(lab, 2, quality=review)  # recorded after the benchmark, without one
    record_comparison(lab.layout, judged)
    records = read_comparisons(lab.layout)
    assert len(records) == 3 and read_comparisons(lab.layout, ("other",)) == []
    (row,) = comparison_rows(records)
    # The review recorded last does not hide the benchmark recorded before it.
    assert row["real_time_factor"] == 10.0 and row["blind_review"] == "review-1"
    assert row["benchmark_comparison_id"] == measured.comparison_id
    assert row["review_comparison_id"] == judged.comparison_id
    assert row["training_comparison_id"] == judged.comparison_id
    assert row["benchmark_recorded_at"] == measured.created_at.isoformat().replace("+00:00", "Z")
    assert row["review_recorded_at"] != row["benchmark_recorded_at"]
    assert row["review_overall_mean"] == 4.2 and row["corpus_differs"] is False
    assert row["peak_memory_method"] is None and row["sandboxed"] is False
    assert row["gpu_shared_with"] == [] and comparison_warnings([row]) == [
        "finetune-a's benchmark ran outside the speech sandbox"
    ]
    with pytest.raises(ValueError, match="its run's"):
        ModelComparison.model_validate({**first.model_dump(), "run_id": "other"})


def test_compare_flags_runs_on_another_corpus_and_unlike_hosts(lab: Lab) -> None:
    base = comparison(lab, 0)
    rows = comparison_rows(
        [
            base,
            base.model_copy(
                update={
                    "comparison_id": "mc-b",
                    "run_id": "finetune-b",
                    "training": base.training.model_copy(
                        update={"run_id": "finetune-b", "host": HOST}
                    ),
                    "inference": benchmark(lab, contention=lambda: BUSY, sandboxed=True),
                }
            ),
            base.model_copy(
                update={
                    "comparison_id": "mc-c",
                    "run_id": "finetune-c",
                    "training": base.training.model_copy(
                        update={
                            "run_id": "finetune-c",
                            "corpus_sha256": "c" * 64,
                            "host": HOST.model_copy(update={"cpu_model": "Other CPU"}),
                        }
                    ),
                    "inference": benchmark(
                        lab,
                        host=lambda: HOST.model_copy(update={"rocm_version": None}),
                        sandboxed=True,
                    ),
                }
            ),
        ]
    )
    assert [row["corpus_differs"] for row in rows] == [False, False, True]
    assert "Synthetic CPU 8000" in str(rows[1]["training_host"])
    assert "ROCm 7.0.0" in str(rows[1]["benchmark_host"])
    warnings = comparison_warnings(rows)
    assert warnings[0].startswith("finetune-c trained on another corpus")
    assert "the benchmarks ran on different hosts" in warnings
    assert "the runs trained on different hosts" in warnings
    assert "finetune-b's benchmark shared the GPU with llama-server" in warnings
    unknown = GpuContention(vram_used_bytes=None, vram_total_bytes=None, gpu_processes=None)
    blind = benchmark(lab, contention=lambda: unknown, sandboxed=True)
    (row,) = comparison_rows([base.model_copy(update={"inference": blind})])
    assert row["gpu_shared_with"] is None and comparison_warnings([row]) == []


def sheet(review_id: str = "review-1") -> bytes:
    return json.dumps({"review_id": review_id, "synthetic": True}).encode()


def test_a_review_link_is_made_from_the_verified_score_sheet(
    lab: Lab, monkeypatch: pytest.MonkeyPatch
) -> None:
    checked: list[str] = []

    class Sheet(BaseModel):
        model_config = ConfigDict(extra="allow")
        review_id: str

    def aggregate(layout: Any, parsed: Any) -> dict[str, object]:
        checked.append(parsed.review_id)
        candidate = {
            "candidate_id": "finetune-a",
            "track": "direct-tts",
            "sample_count": 6,
            "means": {"likeness": 4.0, "naturalness": 3.5},
            "overall_mean": 3.75,
        }
        return {"tracks": {"direct-tts": {"candidates": [candidate]}}}

    monkeypatch.setattr(model_comparison, "ScoreSheet", Sheet)
    monkeypatch.setattr(model_comparison, "aggregate_scores", aggregate)
    data = sheet()
    digest = hashlib.sha256(data).hexdigest()

    def link(**changes: Any) -> QualityLink:
        options: dict[str, Any] = {
            "score_sheet_sha256": digest,
            "candidate_id": None,
            "run_id": "finetune-a",
            **changes,
        }
        return blind_review_link(lab.layout, "review-1", options.pop("data", data), **options)

    made = link()
    assert (made.candidate_id, made.sample_count, made.overall_mean) == ("finetune-a", 6, 3.75)
    assert made.means == {"likeness": 4.0, "naturalness": 3.5} and checked == ["review-1"]
    with pytest.raises(ComparisonError, match="digest"):
        link(score_sheet_sha256="0" * 64)
    other = sheet("review-2")
    with pytest.raises(ComparisonError, match="review review-2's"):
        link(data=other, score_sheet_sha256=hashlib.sha256(other).hexdigest())
    broken = b'{"synthetic": true}'
    with pytest.raises(ComparisonError, match="not a score sheet"):
        link(data=broken, score_sheet_sha256=hashlib.sha256(broken).hexdigest())
    # A review that did not score the run is never linked to it.
    with pytest.raises(ComparisonError, match="did not judge run finetune-b.*--review-candidate"):
        link(run_id="finetune-b")
    with pytest.raises(ComparisonError, match="no candidate nobody"):
        link(candidate_id="nobody")


def test_in_speech_sandbox_means_the_units_marker_and_no_network_but_loopback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("VOICE_LAB_SANDBOX", raising=False)
    assert not cli_models.in_speech_sandbox(lambda: ("lo",))  # `unshare -rn` alone: no
    monkeypatch.setenv("VOICE_LAB_SANDBOX", "something-else")
    assert not cli_models.in_speech_sandbox(lambda: ("lo",))
    monkeypatch.setenv("VOICE_LAB_SANDBOX", "voice-lab-speech-benchmark")
    assert cli_models.in_speech_sandbox(lambda: ("lo",))
    assert not cli_models.in_speech_sandbox(lambda: ("eth0", "lo"))

    def unlistable() -> tuple[str, ...]:
        raise OSError("no netlink")

    assert not cli_models.in_speech_sandbox(unlistable)
    devices = tmp_path / "dev"
    devices.write_text(
        "Inter-|   Receive |  Transmit\n face |bytes    packets\n"
        "    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
        "  eth0: 200 2 0 0 0 0 0 0 200 2 0 0 0 0 0 0\n",
        encoding="ascii",
    )
    assert cli_models.network_interfaces(devices) == ("eth0", "lo")
    with pytest.raises(OSError):
        cli_models.network_interfaces(tmp_path / "missing")


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
    monkeypatch.setattr(model_comparison, "host_fingerprint", lambda: HOST)
    monkeypatch.setattr(model_comparison, "gpu_contention", lambda: QUIET)
    slot = Path(lab.config.gpu_slot_root)
    argv = [*base, "--benchmark", "--config", str(config), "--consent-id", "cole-self-v1"]
    # This test process has a network: the benchmark is refused outside the sandbox.
    monkeypatch.setattr(cli_models, "network_interfaces", lambda: ("eth0", "lo"))
    monkeypatch.setenv("VOICE_LAB_SANDBOX", "voice-lab-speech-benchmark")
    assert main(argv) == 2
    assert "--i-render-outside-the-sandbox" in capsys.readouterr().err and not seen
    assert main([*argv, "--i-render-outside-the-sandbox"]) == 0
    outside = json.loads(capsys.readouterr().out)
    assert outside["inference"]["sandboxed"] is False
    monkeypatch.setattr(cli_models, "network_interfaces", lambda: ("lo",))
    state = Path(lab.config.state_root) / "benchmark"
    leftover = state / ".model-benchmark-killed"
    leftover.mkdir(parents=True)
    lab_tmp = sorted((lab.layout.root / "tmp").iterdir())
    assert main(argv) == 0
    benchmarked = json.loads(capsys.readouterr().out)
    assert benchmarked["inference"]["deterministic"] is True
    assert benchmarked["inference"]["sandboxed"] is True and not leftover.exists()
    # Its segments never touched the lab's tmp/ (raw take originals live there).
    assert sorted((lab.layout.root / "tmp").iterdir()) == lab_tmp
    assert seen[1][1].unloads == 2 and not slot_wanted(slot) and current_holder(slot) is None
    # The benchmark's backend keeps its scratch apart from the speech runner's.
    assert seen[1][0].state_root == str(Path(lab.config.state_root) / "benchmark")

    data = sheet()
    sheet_path = lab.root / "scores.json"
    sheet_path.write_bytes(data)
    monkeypatch.setattr(
        cli_models,
        "blind_review_link",
        lambda layout, review_id, raw, **options: (
            QualityLink(
                review_id=review_id,
                score_sheet_sha256=options["score_sheet_sha256"],
                candidate_id="finetune-a",
                sample_count=6,
                overall_mean=3.5,
                means={"likeness": 3.5},
            )
            if raw == data
            else pytest.fail("the sheet was not read")
        ),
    )
    review = [
        "--blind-review",
        "review-1",
        "--score-sheet",
        str(sheet_path),
        "--score-sheet-sha256",
        hashlib.sha256(data).hexdigest(),
    ]
    assert main([*base, *review[:2]]) == 2
    assert "go together" in capsys.readouterr().err
    assert main([*base, "--review-candidate", "x"]) == 2
    assert "--review-candidate" in capsys.readouterr().err
    assert main([*base, *review]) == 0
    capsys.readouterr()
    assert main(["families", "compare", "--asset-root", root]) == 0
    compared = json.loads(capsys.readouterr().out)
    (row,) = compared["rows"]
    assert row["run_id"] == "finetune-a" and row["blind_review"] == "review-1"
    # The newest benchmark (sandboxed) survives the review recorded after it.
    assert row["real_time_factor"] is not None and row["sandboxed"] is True
    assert compared["warnings"] == []


def test_a_score_sheet_is_read_bounded(lab: Lab, monkeypatch: pytest.MonkeyPatch) -> None:
    big = lab.root / "big.json"
    big.write_bytes(b" " * 16)
    monkeypatch.setattr(cli_models, "MAX_SCORE_SHEET_BYTES", 8)
    with pytest.raises(cli_models.VoiceLabError, match="exceeds"):
        cli_models._read_score_sheet(big)
