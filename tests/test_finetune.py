"""The fine-tuning corpus is the latest retained take per spoken prompt (finetune.py)."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import wave
from array import array
from datetime import timedelta
from pathlib import Path
from typing import Any, get_args

import pytest
from finetune_stub_runtime import stub_finetune_runtime
from recording_fixtures import write_synthetic_wav
from test_pilot_v2_coverage import (
    SUBJECT,
    UMIK,
    NativeCoverage,
    build_legacy_root,
    released_pilot_v2,
    spoken,
)
from test_pilot_v2_coverage import analyzer_build as analyzer_build
from test_pilot_v2_coverage import native_coverage as native_coverage
from test_prompt_coverage import identity_changing_alias
from test_recording_index_backfill import CATALOG_ID as BACKFILL_CATALOG_ID
from test_recording_index_backfill import Published
from test_recording_index_backfill import published as published
from test_recording_library import START
from test_recording_packages import saved_take
from test_recording_takes import Rig, publish_catalog, store_suite

import cole_voice_lab.finetune as finetune_module
from cole_voice_lab.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    sha256_file,
    write_canonical_json,
)
from cole_voice_lab.catalog_ports import CorpusSnapshotSecurityError
from cole_voice_lab.cli import main
from cole_voice_lab.finetune import (
    FineTuneCorpus,
    FineTuneCorpusEntry,
    FineTuneDataset,
    FineTuneDatasetClip,
    FineTuneDownload,
    FineTuneError,
    FineTuneErrorCode,
    FineTuneHyperparameters,
    FineTunePinnedFile,
    FineTunePipelineStep,
    FineTunePretrainedPins,
    FineTuneRun,
    FineTuneRuntimeIdentity,
    FineTuneSample,
    FineTuneSampleSet,
    describe_finetune_runtime,
    download_verified,
    execute_finetune_runtime_provisioning,
    fetch_finetune_pretrained,
    finetune_cli_arguments,
    finetune_pretrained_pins,
    finetune_run_summary,
    latest_finished_finetune_run,
    mark_finetune_run_failed,
    materialize_finetune_dataset,
    normalization_gain_db,
    plan_finetune_runtime_provisioning,
    release_finetune_runtime_link,
    require_complete_corpus,
    run_f5_finetune,
    run_finetune_pipeline,
    select_finetune_corpus,
    synthesize_finetune_samples,
)
from cole_voice_lab.models import PromptSuite, utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_analysis import RecordingAnalysisQueue
from cole_voice_lab.recording_contracts import (
    CatalogReference,
    CatalogSnapshot,
    ErrorCode,
    RecordingError,
    RecordingFrameRange,
    catalog_snapshot_path,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_takes import RecordingTakeStore
from cole_voice_lab.storage_binding import open_catalog_editor, open_catalog_reader

SPOKEN_PROMPT_COUNT = 34


def test_every_finetune_error_code_is_exactly_the_wire_finetune_codes() -> None:
    codes = set(get_args(FineTuneErrorCode.__value__))
    wire = {code for code in get_args(ErrorCode.__value__) if code.startswith("FINETUNE.")}
    assert codes == wire
    # Every code the module names is in the closed set, and so are the job path's refusals.
    source = Path(finetune_module.__file__).read_text(encoding="utf-8")
    body = source.split("class FineTuneError(VoiceLabError):", 1)[1]
    assert set(re.findall(r'"(FINETUNE\.[A-Z_]+)"', body)) <= codes
    assert {"FINETUNE.CORPUS_INCOMPLETE", "FINETUNE.INTERRUPTED", "FINETUNE.JOB_ACTIVE"} <= codes
    # Every code is sent by something besides the two closed lists; a cancelled job carries
    # no error, so there is no FINETUNE.CANCELLED.
    assert "FINETUNE.CANCELLED" not in codes
    package = Path(finetune_module.__file__).parent
    sources = [path.read_text(encoding="utf-8") for path in sorted(package.glob("*.py"))]
    for code in codes:
        assert sum(text.count(f'"{code}"') for text in sources) >= 3, f"nothing sends {code}"
    for code in codes:
        error = FineTuneError(code, "Synthetic")
        wired = RecordingError(code=error.code, message=str(error), retryable=False)
        assert (wired.code, wired.message) == (code, "Synthetic")


def _entry(corpus: FineTuneCorpus, prompt_id: str) -> FineTuneCorpusEntry:
    return next(entry for entry in corpus.entries if entry.prompt_id == prompt_id)


def test_native_kept_take_is_selected_and_room_tones_never_appear(
    native_coverage: NativeCoverage,
) -> None:
    catalog = native_coverage.pilot_v2
    corpus = select_finetune_corpus(native_coverage.rig.layout, catalog, SUBJECT)
    assert corpus.catalog_sha256 == canonical_sha256(catalog)
    assert [entry.prompt_id for entry in corpus.entries] == [spoken(1)]
    entry = corpus.entries[0]
    seal = native_coverage.spoken.seal
    assert seal is not None
    assert entry.source_kind == "native"
    assert entry.take_id == native_coverage.spoken.reservation.take_id
    assert (entry.source_path, entry.source_sha256) == (seal.original_path, seal.original_sha256)
    assert entry.recorded_at == seal.first_sample_at and entry.date_precision == "take"
    assert entry.recorded_prompt_sha256 == entry.prompt_sha256
    assert corpus.catalog_reference is None
    assert len(corpus.missing_prompt_ids) == SPOKEN_PROMPT_COUNT - 1
    assert "room-tone-start" not in corpus.missing_prompt_ids
    assert corpus.unknown_record_ids == () and not corpus.complete


def test_native_draft_is_not_selected(native_coverage: NativeCoverage) -> None:
    rig = native_coverage.rig
    rig.now = utc_now() - timedelta(seconds=5)
    rig.store.clock = lambda: rig.now
    rig.seal(rig.reserve("reserve-draft", spoken(2)), "seal-draft")
    corpus = select_finetune_corpus(rig.layout, native_coverage.pilot_v2, SUBJECT)
    assert spoken(2) in corpus.missing_prompt_ids


def test_trim_recipe_is_carried_into_the_corpus(native_coverage: NativeCoverage) -> None:
    rig = native_coverage.rig
    rig.now = utc_now() - timedelta(seconds=5)
    rig.store.clock = lambda: rig.now
    draft = rig.seal(rig.reserve("reserve-trim", spoken(2)), "seal-trim")
    trim = RecordingFrameRange(source_frames=480, start_frame=24, end_frame=400)
    rig.store.save(rig.command(draft, operation="save-trim", frame_range=trim))  # keep with trim
    corpus = select_finetune_corpus(rig.layout, native_coverage.pilot_v2, SUBJECT)
    entry = _entry(corpus, spoken(2))
    assert entry.frame_range == trim
    assert entry.retained_frames == 376 and entry.source_frames == 480


def test_legacy_latest_kept_take_wins_and_rejected_or_removed_takes_never_do(
    tmp_path: Path,
) -> None:
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    corpus = select_finetune_corpus(evidence.layout, released_pilot_v2(), SUBJECT)
    assert corpus.missing_prompt_ids == () and corpus.unknown_record_ids == ()
    assert len(corpus.entries) == SPOKEN_PROMPT_COUNT and corpus.complete
    assert all(entry.source_kind == "legacy" for entry in corpus.entries)
    assert not any(".rejected" in entry.source_path for entry in corpus.entries)
    # spoken 1: kept in the sidecar-less session, the finalized session and the in-progress
    # session; the in-progress take is the newest kept one.
    assert _entry(corpus, spoken(1)).source_path.startswith(f"tmp/capture-{evidence.in_progress}/")
    # spoken 2: the newer finalized take was removed, so the sidecar-less take is current.
    assert _entry(corpus, spoken(2)).source_path.startswith("tmp/capture-legacy-pilot/")
    # spoken 3: the in-progress session only holds an interrupted draft for it.
    assert _entry(corpus, spoken(3)).source_path.startswith("tmp/capture-legacy-pilot/")
    # spoken 4: kept in an unfinished session still counts, exactly as coverage counts it.
    assert _entry(corpus, spoken(4)).source_path.startswith(f"tmp/capture-{evidence.ready_later}/")
    for entry in corpus.entries:
        assert entry.recorded_at is not None and entry.date_precision in ("take", "session")


def _native_store_over_legacy_root(layout: AssetLayout, analyzer_build: Any, catalog: PromptSuite):
    # The group opens before every take it will hold, including the deliberately old one.
    rig = Rig(layout, RecordingTakeStore(layout), "", now=START - timedelta(days=2))
    rig.store = RecordingTakeStore(layout, clock=lambda: rig.now)
    rig.group_id = rig.store.open_group(catalog, SUBJECT, UMIK, "synthetic-host").group_id
    index = RecordingIndex(layout, subject_id=SUBJECT)
    rig.store.projection_hook = index.update_take
    queue = RecordingAnalysisQueue(rig.store, index, build=analyzer_build)
    return rig, index, queue


def test_newer_native_take_beats_legacy_and_older_native_take_loses(
    tmp_path: Path, analyzer_build: Any
) -> None:
    evidence = build_legacy_root(tmp_path.resolve() / "assets")
    catalog = released_pilot_v2()
    rig, index, queue = _native_store_over_legacy_root(evidence.layout, analyzer_build, catalog)
    try:
        newer = saved_take(rig, queue, 1, prompt=spoken(1))  # recorded now, after every legacy take
        rig.now = START - timedelta(days=1)
        rig.store.clock = lambda: rig.now
        older = rig.seal(rig.reserve("reserve-old", spoken(2)), "seal-old")
        older = rig.store.save(rig.command(older, operation="save-old")).take
    finally:
        queue.close()
        index.close()
    corpus = select_finetune_corpus(evidence.layout, catalog, SUBJECT)
    first, second = _entry(corpus, spoken(1)), _entry(corpus, spoken(2))
    assert (first.source_kind, first.take_id) == ("native", newer.reservation.take_id)
    assert second.source_kind == "legacy"
    assert older.decision == "kept"
    assert second.recorded_at is not None and older.seal is not None
    assert second.recorded_at > older.seal.first_sample_at


def test_corpus_entry_contract_refuses_silent_prompts_and_foreign_prompt_digests() -> None:
    catalog = released_pilot_v2()
    tone = next(prompt for prompt in catalog.prompts if prompt.category == "room-tone")
    said = next(prompt for prompt in catalog.prompts if prompt.category != "room-tone")
    base: dict[str, Any] = dict(
        recorded_prompt_sha256="2" * 64,
        take_id="take-x",
        source_kind="native",
        source_path="tmp/recording-takes/take-x/original.wav",
        source_sha256="0" * 64,
        source_frames=480,
        source_sample_rate_hz=48_000,
        recorded_at=None,
        date_precision="unknown",
    )
    with pytest.raises(ValueError, match="spoken"):
        FineTuneCorpusEntry(
            prompt_id=tone.prompt_id, prompt_sha256=canonical_sha256(tone), prompt=tone, **base
        )
    with pytest.raises(ValueError, match="digest"):
        FineTuneCorpusEntry(prompt_id=said.prompt_id, prompt_sha256="1" * 64, prompt=said, **base)
    with pytest.raises(ValueError, match="exact source"):
        FineTuneCorpusEntry(
            prompt_id=said.prompt_id,
            prompt_sha256=canonical_sha256(said),
            prompt=said,
            frame_range=RecordingFrameRange(source_frames=100, start_frame=0, end_frame=50),
            **base,
        )


def _corpus_with_one_entry(**changes: Any) -> FineTuneCorpus:
    catalog = released_pilot_v2()
    said = next(prompt for prompt in catalog.prompts if prompt.category != "room-tone")
    entry = FineTuneCorpusEntry(
        prompt_id=said.prompt_id,
        prompt_sha256=canonical_sha256(said),
        recorded_prompt_sha256="2" * 64,
        prompt=said,
        take_id="take-x",
        source_kind="native",
        source_path="tmp/recording-takes/take-x/original.wav",
        source_sha256="0" * 64,
        source_frames=480,
        source_sample_rate_hz=48_000,
        recorded_at=None,
        date_precision="unknown",
    )
    fields: dict[str, Any] = dict(
        subject_id=SUBJECT,
        catalog_suite_id=catalog.suite_id,
        catalog_sha256=canonical_sha256(catalog),
        entries=(entry,),
        missing_prompt_ids=(),
        unknown_record_ids=(),
        selected_at=START,
    )
    return FineTuneCorpus(**(fields | changes))


def test_corpus_entry_names_its_recorded_digest_and_older_corpora_keep_their_bytes() -> None:
    corpus = _corpus_with_one_entry()
    entry = corpus.entries[0]
    assert entry.recorded_prompt_sha256 == "2" * 64 != entry.prompt_sha256
    payload = json.loads(canonical_json_bytes(corpus))
    assert payload["entries"][0]["recorded_prompt_sha256"] == "2" * 64
    assert "catalog_reference" not in payload
    # A corpus.json written before recorded digests existed still reads, byte for byte.
    del payload["entries"][0]["recorded_prompt_sha256"]
    older = canonical_json_bytes(payload)
    restored = FineTuneCorpus.model_validate_json(older)
    assert restored.entries[0].recorded_prompt_sha256 is None
    assert canonical_json_bytes(restored) == older


def test_corpus_catalog_reference_must_name_the_selected_catalog() -> None:
    catalog = released_pilot_v2()
    digest = canonical_sha256(catalog)
    snapshot_bytes = canonical_json_bytes(CatalogSnapshot.embedded(catalog)) + b"\n"
    reference = CatalogReference(
        catalog_id="cole-corpus",
        catalog_version=1,
        catalog_sha256=digest,
        snapshot_sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
        suite_id=catalog.suite_id,
        purpose=catalog.purpose,
        snapshot_path=catalog_snapshot_path(digest),
    )
    corpus = _corpus_with_one_entry(catalog_reference=reference)
    assert FineTuneCorpus.model_validate_json(canonical_json_bytes(corpus)) == corpus
    assert json.loads(canonical_json_bytes(corpus))["catalog_reference"]["catalog_version"] == 1
    for changes in ({"catalog_sha256": "0" * 64}, {"catalog_suite_id": "another-suite"}):
        with pytest.raises(ValueError, match="exact selected catalog"):
            _corpus_with_one_entry(catalog_reference=reference, **changes)


def test_selection_names_a_published_version_only_when_it_seals_the_selected_catalog(
    tmp_path: Path,
) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    first, plain = publish_catalog(layout, store_suite(2))
    with open_catalog_editor(layout) as corpus:
        corpus.edit_prompt(
            "spoken-0001",
            text="Synthetic sentence one.",
            keep_recordings=True,
            allow_rewording=True,
        )
        second = corpus.publish_version("synthetic-corpus")
        aliased = corpus.materialize(second)
    corpus_of = select_finetune_corpus(layout, aliased, SUBJECT, reference=second)
    assert corpus_of.catalog_reference == second
    assert corpus_of.catalog_sha256 == second.catalog_sha256
    assert corpus_of.catalog_suite_id == second.suite_id == "synthetic-corpus"
    assert corpus_of.missing_prompt_ids == ("spoken-0000", "spoken-0001")
    # A version without aliases is exactly its plain suite.
    assert (
        select_finetune_corpus(layout, plain.suite, SUBJECT, reference=first).catalog_reference
        == first
    )
    assert select_finetune_corpus(layout, aliased, SUBJECT).catalog_reference is None
    for catalog, reference in (
        (aliased.suite, second),  # the version's aliases would silently stop counting
        (aliased, first),
        (plain, second),
        (released_pilot_v2(), first),
    ):
        with pytest.raises(CorpusSnapshotSecurityError) as caught:
            select_finetune_corpus(layout, catalog, SUBJECT, reference=reference)
        assert caught.value.code == "CORPUS.SNAPSHOT_MISMATCH"


@pytest.mark.parametrize("change", ["holdout", "silent-to-spoken"])
def test_held_out_or_silent_recordings_never_train_through_an_alias(
    tmp_path: Path, change: str
) -> None:
    case = identity_changing_alias(tmp_path.resolve() / "assets", change)
    corpus = select_finetune_corpus(case.layout, case.snapshot, SUBJECT)
    assert case.prompt_id in corpus.missing_prompt_ids
    assert case.take.reservation.take_id not in {entry.take_id for entry in corpus.entries}
    if change == "holdout":
        # The catalog the take was recorded against is evaluation material: never a corpus.
        with pytest.raises(FineTuneError, match="held-out evaluation material") as caught:
            select_finetune_corpus(case.layout, case.recorded_suite, SUBJECT)
        assert caught.value.code == "FINETUNE.CATALOG_HELD_OUT"


def test_cli_refuses_to_select_a_corpus_from_a_held_out_catalog(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = AssetLayout.initialize(tmp_path.resolve() / "assets").root
    command = ["finetune", "corpus", "--asset-root", str(root), "--catalog", "evaluation-v1"]
    assert main([*command, "--allow-missing"]) == 2
    assert "held-out evaluation material" in capsys.readouterr().err


def test_legacy_takes_keep_their_selection_through_keep_recordings_aliases(
    published: Published,
) -> None:
    """TC2 under published versions: reworded prompts keep their takes, with the new text."""
    layout = published.evidence.layout
    before = select_finetune_corpus(layout, released_pilot_v2(), SUBJECT)
    assert before.complete
    with open_catalog_reader(layout) as corpus:
        seeded_reference, kept_reference, changed_reference = (
            corpus.resolve_reference(BACKFILL_CATALOG_ID, version) for version in (1, 2, 3)
        )
    seeded = select_finetune_corpus(layout, published.seeded, SUBJECT, reference=seeded_reference)
    assert seeded.entries == before.entries and seeded.complete
    assert seeded.catalog_sha256 == published.seeded.catalog_sha256 != before.catalog_sha256
    assert seeded.catalog_reference == seeded_reference

    kept = select_finetune_corpus(layout, published.kept, SUBJECT, reference=kept_reference)
    assert kept.complete and kept.catalog_reference == kept_reference
    reworded = {spoken(1), spoken(3)}
    for earlier, entry in zip(before.entries, kept.entries, strict=True):
        assert (entry.prompt_id, entry.take_id, entry.source_path, entry.source_sha256) == (
            earlier.prompt_id,
            earlier.take_id,
            earlier.source_path,
            earlier.source_sha256,
        )
        assert entry.recorded_prompt_sha256 == earlier.prompt_sha256
        if entry.prompt_id in reworded:
            assert entry.prompt == published.kept.prompt(entry.prompt_id)
            assert entry.text == f"{earlier.text} Again."
            assert entry.prompt_sha256 == canonical_sha256(entry.prompt) != earlier.prompt_sha256
        else:
            assert entry == earlier

    changed = select_finetune_corpus(
        layout, published.changed, SUBJECT, reference=changed_reference
    )
    assert changed.missing_prompt_ids == (spoken(2),) and not changed.complete
    assert [entry.prompt_id for entry in changed.entries] == [
        entry.prompt_id for entry in kept.entries if entry.prompt_id != spoken(2)
    ]


FFMPEG = Path("/opt/homebrew/bin/ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG.exists(), reason="ffmpeg is not installed")


def _pcm16_levels(path: Path) -> tuple[int, int, float, float]:
    """(frames, sample_rate, rms_dbfs, peak_dbfs) of a 16-bit mono WAV, standard library only."""
    with wave.open(str(path), "rb") as handle:
        assert (handle.getnchannels(), handle.getsampwidth()) == (1, 2)
        samples = array("h", handle.readframes(handle.getnframes()))
        rate = handle.getframerate()
    scaled = [value / 32768.0 for value in samples]
    rms = math.sqrt(sum(value * value for value in scaled) / len(scaled))
    peak = max(abs(value) for value in scaled)
    return len(scaled), rate, 20 * math.log10(max(rms, 1e-9)), 20 * math.log10(max(peak, 1e-9))


@pytest.mark.parametrize(
    ("rms", "peak", "expected"),
    [(-50.0, -30.0, 29.5), (-50.0, -15.0, 14.5), (-10.0, -3.0, 0.0), (-10.0, 0.0, -0.5)],
)
def test_normalization_gain_raises_rms_to_target_but_never_past_the_peak_ceiling(
    rms: float, peak: float, expected: float
) -> None:
    assert normalization_gain_db(rms, peak) == expected


@needs_ffmpeg
def test_materialized_dataset_is_24khz_mono_normalized_and_hash_bound(
    native_coverage: NativeCoverage,
) -> None:
    rig = native_coverage.rig
    rig.now = utc_now() - timedelta(seconds=5)
    rig.store.clock = lambda: rig.now
    draft = rig.seal(rig.reserve("reserve-trim", spoken(2)), "seal-trim")
    trim = RecordingFrameRange(source_frames=480, start_frame=24, end_frame=400)
    rig.store.save(rig.command(draft, operation="save-trim", frame_range=trim))
    corpus = select_finetune_corpus(rig.layout, native_coverage.pilot_v2, SUBJECT)
    dataset = materialize_finetune_dataset(
        rig.layout, corpus, "derived/finetune/test-run/dataset", ffmpeg=FFMPEG
    )
    root = rig.layout.root / "derived/finetune/test-run/dataset"
    assert dataset.dataset_root == "derived/finetune/test-run/dataset"
    assert dataset.corpus_sha256 == canonical_sha256(corpus)
    assert [clip.prompt_id for clip in dataset.clips] == [spoken(1), spoken(2)]
    for clip in dataset.clips:
        path = root / clip.relative_path
        frames, rate, rms_dbfs, peak_dbfs = _pcm16_levels(path)
        assert (frames, rate) == (clip.frames, 24_000)
        assert sha256_file(path)[0] == clip.sha256
        assert peak_dbfs <= -0.4
        # The synthetic sine already sits above the -20 dBFS target: the rule never turns
        # audio down, so the copy keeps the source level (the trimmed copy holds a partial
        # period, hence the tolerance) and records a zero gain.
        assert clip.gain_db == 0.0 and abs(rms_dbfs + 15.46) < 1.0
    whole, trimmed = dataset.clips
    assert abs(whole.frames - 240) <= 2 and abs(trimmed.frames - 188) <= 2
    lines = (root / "metadata.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "audio_file|text" and len(lines) == 3
    assert lines[1] == f"{(root / 'wavs' / f'{spoken(1)}.wav').resolve()}|{whole.text}"
    assert sha256_file(root / "metadata.csv")[0] == dataset.metadata_csv_sha256
    assert FineTuneDataset.model_validate_json((root / "dataset.json").read_bytes()) == dataset
    assert FineTuneCorpus.model_validate_json((root / "corpus.json").read_bytes()) == corpus


def _write_quiet_24bit_sine(path: Path, *, frames: int, peak: float) -> None:
    """A 48 kHz mono 24-bit sine at the given linear peak, written with the standard library."""
    payload = bytearray()
    for index in range(frames):
        value = round(peak * 8_388_607 * math.sin(index * math.tau / 480))
        payload += value.to_bytes(3, "little", signed=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(3)
        handle.setframerate(48_000)
        handle.writeframes(bytes(payload))


@needs_ffmpeg
def test_quiet_source_is_raised_to_the_target_level(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    catalog = released_pilot_v2()
    prompt = next(item for item in catalog.prompts if item.category != "room-tone")
    source = layout.root / "tmp" / "quiet-source.wav"
    _write_quiet_24bit_sine(source, frames=48_000, peak=10 ** (-47.0 / 20))  # RMS about -50
    corpus = FineTuneCorpus(
        subject_id=SUBJECT,
        catalog_suite_id=catalog.suite_id,
        catalog_sha256=canonical_sha256(catalog),
        entries=(
            FineTuneCorpusEntry(
                prompt_id=prompt.prompt_id,
                prompt_sha256=canonical_sha256(prompt),
                recorded_prompt_sha256=canonical_sha256(prompt),
                prompt=prompt,
                take_id="take-quiet",
                source_kind="native",
                source_path="tmp/quiet-source.wav",
                source_sha256=sha256_file(source)[0],
                source_frames=48_000,
                source_sample_rate_hz=48_000,
                recorded_at=None,
                date_precision="unknown",
            ),
        ),
        missing_prompt_ids=(),
        unknown_record_ids=(),
        selected_at=utc_now(),
    )
    dataset = materialize_finetune_dataset(
        layout, corpus, "derived/finetune/q/dataset", ffmpeg=FFMPEG
    )
    (clip,) = dataset.clips
    frames, rate, rms_dbfs, peak_dbfs = _pcm16_levels(
        layout.root / dataset.dataset_root / clip.relative_path
    )
    assert (frames, rate) == (24_000, 24_000)
    assert 29.0 <= clip.gain_db <= 31.0
    assert abs(rms_dbfs + 20.0) < 0.6 and peak_dbfs <= -0.4


@needs_ffmpeg
def test_materialization_refuses_a_source_that_changed_since_selection(
    native_coverage: NativeCoverage,
) -> None:
    rig = native_coverage.rig
    corpus = select_finetune_corpus(rig.layout, native_coverage.pilot_v2, SUBJECT)
    original = rig.layout.root / corpus.entries[0].source_path
    original.unlink()
    write_synthetic_wav(original, 0.02)
    with pytest.raises(FineTuneError) as failure:
        materialize_finetune_dataset(
            rig.layout, corpus, "derived/finetune/x/dataset", ffmpeg=FFMPEG
        )
    assert failure.value.code == "FINETUNE.SOURCE_CHANGED"
    assert not (rig.layout.root / "derived/finetune/x/dataset/metadata.csv").exists()


_stub_runtime = stub_finetune_runtime


def patch_default_pins(monkeypatch: pytest.MonkeyPatch, pins: FineTunePretrainedPins) -> None:
    """Make the shipped pins the stub runtime's, as a real runtime would match them."""
    for prefix, pin in (
        ("FINETUNE_PRETRAINED", pins.weights),
        ("FINETUNE_VOCODER_CONFIG", pins.vocoder_config),
        ("FINETUNE_VOCODER_MODEL", pins.vocoder_model),
    ):
        monkeypatch.setattr(finetune_module, f"{prefix}_SHA256", pin.sha256)
        monkeypatch.setattr(finetune_module, f"{prefix}_SIZE_BYTES", pin.size_bytes)


def test_the_shipped_pins_name_the_pinned_revisions() -> None:
    pins = finetune_pretrained_pins()
    assert pins.weights == FineTunePinnedFile(
        sha256="670900fd14e6c458b95da6e9ed317cdb20dbaf7a1c02ac06a05475a9d32b6a38",
        size_bytes=1_348_435_761,
    )
    assert pins.vocoder_config == FineTunePinnedFile(
        sha256="da9033922f969a47f0c160010226919e59f27761fd5066f3828d46de6650b0fc",
        size_bytes=461,
    )
    assert pins.vocoder_model == FineTunePinnedFile(
        sha256="97ec976ad1fd67a33ab2682d29c0ac7df85234fae875aefcc5fb215681a91b2a",
        size_bytes=54_365_991,
    )
    assert finetune_module.FINETUNE_VOCODER_REPOSITORY == "charactr/vocos-mel-24khz"
    assert finetune_module.FINETUNE_VOCODER_REVISION == "0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21"


def test_describe_runtime_refuses_wrong_pretrained_weights(tmp_path: Path) -> None:
    root, pin = _stub_runtime(tmp_path / "runtime")
    with pytest.raises(FineTuneError) as failure:
        describe_finetune_runtime(root)  # the real pins, which the stub cannot match
    assert failure.value.code == "FINETUNE.PRETRAINED_MISMATCH"
    runtime = describe_finetune_runtime(root, pretrained_pin=pin)
    assert runtime.identity.f5_tts_version == "1.1.22-stub"
    assert runtime.data_directory == (root / "lib" / "python3.12" / "data").resolve()
    assert runtime.vocoder_path == root.resolve() / "pretrained" / "vocos-mel-24khz"
    with pytest.raises(FineTuneError) as missing:
        describe_finetune_runtime(tmp_path / "nowhere")
    assert missing.value.code == "FINETUNE.RUNTIME_UNAVAILABLE"
    # A probe that reports a repr instead of a path must be refused, not resolved against cwd.
    python = root / "bin" / "python"
    python.write_text(
        python.read_text().replace('"package_path": str(PACKAGE)', '"package_path": repr(PACKAGE)')
    )
    with pytest.raises(FineTuneError) as bogus:
        describe_finetune_runtime(root, pretrained_pin=pin)
    assert bogus.value.code == "FINETUNE.RUNTIME_UNAVAILABLE"
    assert "absolute directory" in str(bogus.value)


@pytest.mark.parametrize("damage", ["config-missing", "model-missing", "size", "digest", "link"])
def test_describe_runtime_checks_the_pinned_vocoder_before_hashing_the_weights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    root, pin = _stub_runtime(tmp_path / "runtime")
    vocoder = root / "pretrained" / "vocos-mel-24khz"
    if damage == "config-missing":
        (vocoder / "config.yaml").unlink()
    elif damage == "model-missing":
        (vocoder / "pytorch_model.bin").unlink()
    elif damage == "size":
        (vocoder / "pytorch_model.bin").write_bytes(b"other-weights-of-another-size")
    elif damage == "link":
        # A cache snapshot's file is a link to its blob; the runtime needs the file itself.
        blob = tmp_path / "blob"
        (vocoder / "pytorch_model.bin").rename(blob)
        (vocoder / "pytorch_model.bin").symlink_to(blob)
    else:
        (vocoder / "pytorch_model.bin").write_bytes(b"vocoder-weightz")  # same size
    hashed: list[str] = []
    original = finetune_module.sha256_file

    def recording_hash(path: Path) -> tuple[str, int]:
        hashed.append(path.name)
        return original(path)

    monkeypatch.setattr(finetune_module, "sha256_file", recording_hash)
    with pytest.raises(FineTuneError) as failure:
        describe_finetune_runtime(root, pretrained_pin=pin)
    message = str(failure.value)
    if damage.endswith("missing"):
        missing = "config.yaml" if damage == "config-missing" else "pytorch_model.bin"
        assert failure.value.code == "FINETUNE.RUNTIME_UNAVAILABLE"
        assert str(vocoder / missing) in message and "fetch-pretrained" in message
        assert "charactr/vocos-mel-24khz revision 0feb3fdd" in message
        assert hashed == []  # nothing was hashed before the missing file was named
    else:
        assert failure.value.code == "FINETUNE.PRETRAINED_MISMATCH"
        assert str(vocoder / "pytorch_model.bin") in message
        assert "model_1250000.safetensors" not in hashed  # the big weights come last
        assert ("not a link" in message) == (damage == "link")
    # A vocoder file that is a directory is missing too.
    if damage == "config-missing":
        (vocoder / "config.yaml").mkdir()
        with pytest.raises(FineTuneError) as directory:
            describe_finetune_runtime(root, pretrained_pin=pin)
        assert directory.value.code == "FINETUNE.RUNTIME_UNAVAILABLE"


def test_the_trainer_never_loads_a_vocoder_and_synthesis_loads_the_pinned_one() -> None:
    """F5-TTS 1.1.22's finetune_cli loads a vocoder only for --log_samples."""
    import ast

    from cole_voice_lab.resources import resource_path

    argv = finetune_cli_arguments(
        FineTuneHyperparameters(), dataset_name="run-x", pretrained_path=Path("/p/w.st")
    )
    assert not any(argument.startswith("--log_samples") for argument in argv)
    launcher = _load_resource_module("f5_finetune_launcher.py")
    assert launcher.main(["--lab-light-checkpoints", *argv, "--log_samples"]) == 2
    assert launcher.main([*argv, "--log_samples=1"]) == 2
    tree = ast.parse(resource_path("f5_synthesize.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "load_vocoder"
    ]
    assert len(calls) == 1
    keywords = {keyword.arg: ast.unparse(keyword.value) for keyword in calls[0].keywords}
    assert keywords["is_local"] == "True"
    assert keywords["local_path"] == "job['vocoder_path']"


def _load_resource_module(name: str) -> Any:
    import importlib.util

    from cole_voice_lab.resources import resource_path

    spec = importlib.util.spec_from_file_location(f"_resource_{name[:-3]}", resource_path(name))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_finetune_cli_arguments_carry_every_hyperparameter() -> None:
    settings = FineTuneHyperparameters(epochs=7, learning_rate=2e-5, batch_frames=1600)
    argv = finetune_cli_arguments(settings, dataset_name="run-x", pretrained_path=Path("/p/w.st"))
    pairs = {
        flag: argv[index + 1]
        for index, flag in enumerate(argv[:-1])
        if flag.startswith("--") and not argv[index + 1].startswith("--")
    }
    assert "--lab-light-checkpoints" in argv and "--finetune" in argv
    assert pairs["--lab-dataloader-workers"] == "2" and pairs["--dataset_name"] == "run-x"
    assert pairs["--epochs"] == "7" and pairs["--learning_rate"] == "2e-05"
    assert pairs["--batch_size_per_gpu"] == "1600" and pairs["--pretrain"] == "/p/w.st"
    assert pairs["--keep_last_n_checkpoints"] == "1" and pairs["--last_per_updates"] == "180"


@needs_ffmpeg
def test_run_records_checkpoints_and_keeps_runtime_state_out_of_the_library(
    native_coverage: NativeCoverage, tmp_path: Path
) -> None:
    layout = native_coverage.rig.layout
    root, pin = _stub_runtime(tmp_path / "runtime")
    runtime = describe_finetune_runtime(root, pretrained_pin=pin)
    corpus = select_finetune_corpus(layout, native_coverage.pilot_v2, SUBJECT)
    dataset = materialize_finetune_dataset(
        layout, corpus, "derived/finetune/r1/dataset", ffmpeg=FFMPEG
    )
    run = run_f5_finetune(
        layout,
        dataset,
        runtime,
        FineTuneHyperparameters(epochs=1),
        "derived/finetune/r1",
        run_id="r1",
        subject_id=SUBJECT,
        catalog_sha256=corpus.catalog_sha256,
    )
    assert run.status == "finished" and run.finished_at is not None and run.error is None
    assert {(c.kind, c.update) for c in run.checkpoints} == {("last", 0), ("intermediate", 9)}
    run_dir = layout.root / "derived/finetune/r1"
    for checkpoint in run.checkpoints:
        assert sha256_file(layout.root / checkpoint.relative_path)[0] == checkpoint.sha256
    assert (
        run_dir / "checkpoints" / "pretrained_model_1250000.safetensors"
    ).read_bytes() == b"pretrained-weights"
    link = runtime.checkpoints_directory / "r1"
    assert link.is_symlink() and link.resolve() == (run_dir / "checkpoints").resolve()
    assert (runtime.data_directory / "r1_pinyin" / "raw.arrow").exists()
    assert (runtime.data_directory / "Emilia_ZH_EN_pinyin" / "vocab.txt").read_text() == "a\nb\n"
    log = (run_dir / "train.log").read_text()
    assert "packed 1 rows" in log and "Saved last checkpoint" in log and "--epochs 1" in log
    assert FineTuneRun.model_validate_json((run_dir / "run.json").read_bytes()) == run
    assert run.dataset_sha256 == canonical_sha256(dataset)


@needs_ffmpeg
def test_failed_training_writes_a_failed_run_record(
    native_coverage: NativeCoverage, tmp_path: Path
) -> None:
    layout = native_coverage.rig.layout
    root, pin = _stub_runtime(tmp_path / "runtime")
    runtime = describe_finetune_runtime(root, pretrained_pin=pin)
    corpus = select_finetune_corpus(layout, native_coverage.pilot_v2, SUBJECT)
    dataset = materialize_finetune_dataset(
        layout, corpus, "derived/finetune/f/dataset", ffmpeg=FFMPEG
    )
    with pytest.raises(FineTuneError) as failure:
        run_f5_finetune(
            layout,
            dataset,
            runtime,
            FineTuneHyperparameters(),
            "derived/finetune/fail-run",
            run_id="fail-run",
            subject_id=SUBJECT,
            catalog_sha256=corpus.catalog_sha256,
        )
    assert failure.value.code == "FINETUNE.TRAINING_FAILED"
    record = FineTuneRun.model_validate_json(
        (layout.root / "derived/finetune/fail-run/run.json").read_bytes()
    )
    assert record.status == "failed" and record.error is not None and record.checkpoints == ()


@needs_ffmpeg
def test_synthesized_samples_are_bound_to_checkpoint_and_reference(
    native_coverage: NativeCoverage, tmp_path: Path
) -> None:
    layout = native_coverage.rig.layout
    root, pin = _stub_runtime(tmp_path / "runtime")
    runtime = describe_finetune_runtime(root, pretrained_pin=pin)
    corpus = select_finetune_corpus(layout, native_coverage.pilot_v2, SUBJECT)
    dataset = materialize_finetune_dataset(
        layout, corpus, "derived/finetune/s/dataset", ffmpeg=FFMPEG
    )
    samples = synthesize_finetune_samples(
        layout,
        dataset,
        runtime,
        runtime.pretrained_path,
        "derived/finetune/s/samples/base",
        label="base",
        reference_prompt_id=spoken(1),
        texts=("Hello there.", "Second line."),
        device="cpu",
        nfe_steps=4,
    )
    job = json.loads((layout.root / "derived/finetune/s/samples/base/job.json").read_text())
    assert job["vocoder_path"] == str(runtime.vocoder_path)
    work = layout.root / "derived/finetune/s/samples/base/synthesis-tmp"
    # The synthesizer's private temporary directory is inside the output and gone afterwards.
    assert job["work_directory"] == str(work) and not os.path.lexists(work)
    assert (
        samples.checkpoint_sha256 == pin.weights.sha256
        and samples.reference_audio_sha256 == dataset.clips[0].sha256
    )
    assert [s.text for s in samples.samples] == ["Hello there.", "Second line."]
    for sample in samples.samples:
        assert sha256_file(layout.root / sample.relative_path)[0] == sample.sha256
    directory = layout.root / "derived/finetune/s/samples/base"
    assert (
        FineTuneSampleSet.model_validate_json((directory / "samples.json").read_bytes()) == samples
    )
    with pytest.raises(FineTuneError) as failure:
        synthesize_finetune_samples(
            layout,
            dataset,
            runtime,
            runtime.pretrained_path,
            "derived/finetune/s/samples/x",
            label="x",
            reference_prompt_id="not-a-prompt",
        )
    assert failure.value.code == "FINETUNE.REFERENCE_MISSING"
    # A synthesizer that leaves its work directory (a copy of the reference) fails by name.
    (root / "stub-mode.json").write_text(json.dumps({"synthesis_leaves_work": True}))
    with pytest.raises(FineTuneError) as left:
        synthesize_finetune_samples(
            layout,
            dataset,
            runtime,
            runtime.pretrained_path,
            "derived/finetune/s/samples/left",
            label="left",
            reference_prompt_id=spoken(1),
            texts=("Hello there.",),
        )
    assert left.value.code == "FINETUNE.RUNTIME_FAILED"
    assert "sample synthesis for left left its work directory" in str(left.value)
    assert (layout.root / "derived/finetune/s/samples/left/synthesis-tmp").is_dir()


FAKE_F5_PACKAGE = {
    "f5_tts/__init__.py": "",
    "f5_tts/configs/F5TTS_v1_Base.yaml": "model: {arch: {}}\n",
    "f5_tts/model/__init__.py": "class DiT:\n    pass\n",
    "f5_tts/infer/__init__.py": "",
    # F5-TTS 1.1.22's preprocess_ref_audio_text: a named temporary file deleted on leaving the
    # block, whose path the converted reference copy is then written to and never removed.
    "f5_tts/infer/utils_infer.py": """
import json, os, tempfile

def load_vocoder(**kwargs):
    return "vocoder"

def load_model(*args, **kwargs):
    return "model"

def preprocess_ref_audio_text(path, text, show_info=print):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete_on_close=False) as handle:
        temp_path = handle.name
    with open(path, "rb") as source, open(temp_path, "wb") as copy:
        copy.write(source.read())
    directory = os.path.dirname(temp_path)
    with open(os.environ["FAKE_F5_REPORT"], "w") as report:
        json.dump({"temp_path": temp_path, "tempdir": tempfile.gettempdir(),
                   "TMPDIR": os.environ.get("TMPDIR"),
                   "mode": os.stat(directory).st_mode & 0o777}, report)
    return temp_path, text

def infer_process(reference_audio, reference_text, text, model, vocoder, **kwargs):
    assert os.path.exists(reference_audio)
    if text == "fail":
        raise RuntimeError("synthetic inference failure")
    return [0] * 240, 24000, None
""",
    "soundfile/__init__.py": """
import wave as _wave

def write(path, samples, rate, subtype):
    assert subtype == "PCM_16"
    with _wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(2 * len(samples)))
""",
    "omegaconf/__init__.py": """
import os
from types import SimpleNamespace

class OmegaConf:
    @staticmethod
    def load(path):
        assert os.path.exists(path), path
        return SimpleNamespace(model=SimpleNamespace(arch={}))
""",
}
# Runs a resource script with the fake packages first on the path, isolated from the
# environment's Python settings.
RUN_WITH_FAKES = (
    "import runpy, sys; sys.path.insert(0, sys.argv[1]); script = sys.argv[2]; "
    "sys.argv = [script, *sys.argv[3:]]; runpy.run_path(script, run_name='__main__')"
)


def test_the_synthesizer_keeps_f5_temporary_copies_in_its_private_work_directory(
    tmp_path: Path,
) -> None:
    import subprocess
    import sys

    from finetune_speech_runs import write_wav

    from cole_voice_lab.resources import resource_path

    fakes = tmp_path / "fakes"
    for relative, text in FAKE_F5_PACKAGE.items():
        (fakes / relative).parent.mkdir(parents=True, exist_ok=True)
        (fakes / relative).write_text(text)
    system_temporary = tmp_path / "system-tmp"
    system_temporary.mkdir()
    reference = tmp_path / "reference.wav"
    write_wav(reference)
    report = tmp_path / "report.json"

    def synthesize(name: str, texts: list[str], **changes: str) -> subprocess.CompletedProcess[str]:
        output = tmp_path / name
        output.mkdir(mode=0o700, exist_ok=True)
        job = {
            "checkpoint_path": str(tmp_path / "model.pt"),
            "vocab_path": str(tmp_path / "vocab.txt"),
            "vocoder_path": str(tmp_path / "vocos"),
            "use_ema": True,
            "device": "cpu",
            "nfe_steps": 4,
            "reference_audio_path": str(reference),
            "reference_text": "Reference words.",
            "output_directory": str(output),
            "work_directory": str(output / "synthesis-tmp"),
            "file_prefix": "speech",
            "texts": texts,
        } | changes
        job_path = output / "job.json"
        job_path.write_text(json.dumps(job))
        report.unlink(missing_ok=True)
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                RUN_WITH_FAKES,
                str(fakes),
                str(resource_path("f5_synthesize.py")),
                str(job_path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=os.environ | {"TMPDIR": str(system_temporary), "FAKE_F5_REPORT": str(report)},
        )

    spoken_job = synthesize("spoken", ["Hello there."])
    assert spoken_job.returncode == 0, spoken_job.stderr
    work = tmp_path / "spoken" / "synthesis-tmp"
    seen = json.loads(report.read_text())
    # F5's temporary copy was written inside the private work directory, not the system's.
    assert Path(seen["temp_path"]).parent == work and seen["mode"] == 0o700
    assert seen["tempdir"] == seen["TMPDIR"] == str(work)
    assert not os.path.lexists(work) and not os.path.lexists(seen["temp_path"])
    assert list(system_temporary.iterdir()) == []
    clip = tmp_path / "spoken" / "speech-01.wav"
    assert json.loads(spoken_job.stdout) == {"index": 1, "path": str(clip), "seconds": 0.01}
    # A failed synthesis removes its work directory too.
    failed = synthesize("failed", ["fail"])
    assert failed.returncode != 0 and "synthetic inference failure" in failed.stderr
    assert Path(json.loads(report.read_text())["temp_path"]).parent.name == "synthesis-tmp"
    assert not os.path.lexists(tmp_path / "failed" / "synthesis-tmp")
    assert list(system_temporary.iterdir()) == []
    # A work directory an interrupted run left is replaced, then removed.
    stale = tmp_path / "spoken" / "synthesis-tmp"
    stale.mkdir()
    (stale / "tmpold.wav").write_bytes(b"old copy")
    again = synthesize("spoken", ["Hello again."])
    assert again.returncode == 0, again.stderr
    assert not os.path.lexists(stale)
    # The work directory must lie directly in the output directory.
    outside = synthesize("outside", ["Hello."], work_directory=str(tmp_path / "elsewhere"))
    assert outside.returncode != 0 and "work_directory must lie directly in" in outside.stderr
    assert not (tmp_path / "elsewhere").exists() and not report.exists()


def test_cli_corpus_reports_the_latest_takes_and_refuses_an_incomplete_corpus(
    tmp_path: Path, native_coverage: NativeCoverage, capsys: pytest.CaptureFixture[str]
) -> None:
    # native_coverage already owns tmp_path/assets; the legacy root must be a separate library.
    evidence = build_legacy_root(tmp_path.resolve() / "legacy" / "assets")
    base = [
        "finetune",
        "corpus",
        "--asset-root",
        str(evidence.layout.root),
        "--catalog",
        "pilot-v2",
    ]
    assert main([*base, "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["selected_prompt_count"] == SPOKEN_PROMPT_COUNT and report["complete"] is True
    assert report["corpus"]["schema_version"] == "cole-voice-lab.finetune-corpus/v1"
    partial = ["finetune", "corpus", "--asset-root", str(native_coverage.rig.layout.root)]
    assert main([*partial, "--catalog", "pilot-v2"]) == 2
    assert "incomplete" in capsys.readouterr().err
    assert main([*partial, "--catalog", "pilot-v2", "--allow-missing"]) == 0
    assert "MISSING" in capsys.readouterr().out


@needs_ffmpeg
def test_cli_run_trains_and_samples_through_a_stub_runtime(
    tmp_path: Path,
    native_coverage: NativeCoverage,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    root, pin = _stub_runtime(tmp_path / "runtime")
    patch_default_pins(monkeypatch, pin)
    layout = native_coverage.rig.layout
    status = main(
        [
            "finetune",
            "run",
            "--asset-root",
            str(layout.root),
            "--catalog",
            "pilot-v2",
            "--allow-missing",
            "--runtime-root",
            str(root),
            "--run-id",
            "cli-run",
            "--epochs",
            "2",
            "--device",
            "cpu",
            "--text",
            "One sentence.",
        ]
    )
    assert status == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "finished" and report["run_root"] == "derived/finetune/cli-run"
    assert report["samples"]["finetuned"] == [
        "derived/finetune/cli-run/samples/finetuned/sample-01.wav"
    ]
    assert report["samples"]["base"] == ["derived/finetune/cli-run/samples/base/sample-01.wav"]
    run = FineTuneRun.model_validate_json(
        (layout.root / "derived/finetune/cli-run/run.json").read_bytes()
    )
    assert (
        run.hyperparameters.epochs == 2 and "--epochs 2" in (layout.root / run.log_path).read_text()
    )
    status = main(
        [
            "finetune",
            "synthesize",
            "--asset-root",
            str(layout.root),
            "--run-id",
            "cli-run",
            "--runtime-root",
            str(root),
            "--checkpoint",
            "base",
            "--label",
            "again",
            "--device",
            "cpu",
            "--text",
            "Again.",
        ]
    )
    assert status == 0
    assert (layout.root / "derived/finetune/cli-run/samples/again/samples.json").exists()


def test_provisioning_plan_names_venv_pins_and_pinned_downloads(tmp_path: Path) -> None:
    pins = tmp_path / "pins.txt"
    pins.write_text("f5-tts==1.1.22\n")
    plan = plan_finetune_runtime_provisioning(
        tmp_path / "rt", python=Path("/usr/bin/python3"), requirements=pins
    )
    assert plan.create_venv is True and plan.install_command[-2:] == ("-r", str(pins))
    urls = [item.url for item in plan.downloads]
    vocos = "https://huggingface.co/charactr/vocos-mel-24khz/resolve/"
    assert urls == [
        "https://huggingface.co/SWivid/F5-TTS/resolve/84e5a410d9cead4de2f847e7c9369a6440bdfaca/"
        "F5TTS_v1_Base/model_1250000.safetensors",
        "https://huggingface.co/SWivid/F5-TTS/resolve/84e5a410d9cead4de2f847e7c9369a6440bdfaca/"
        "F5TTS_v1_Base/vocab.txt",
        vocos + "0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21/config.yaml",
        vocos + "0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21/pytorch_model.bin",
    ]
    root = (tmp_path / "rt").resolve()
    assert [item.destination for item in plan.downloads] == [
        root / "pretrained/F5TTS_v1_Base/model_1250000.safetensors",
        root / "pretrained/F5TTS_v1_Base/vocab.txt",
        root / "pretrained/vocos-mel-24khz/config.yaml",
        root / "pretrained/vocos-mel-24khz/pytorch_model.bin",
    ]
    assert [(item.sha256[:8], item.size_bytes) for item in plan.downloads[2:]] == [
        ("da903392", 461),
        ("97ec976a", 54_365_991),
    ]
    assert all(not item.present for item in plan.downloads)
    assert json.dumps(plan.to_json())  # serializable for the CLI
    with pytest.raises(FineTuneError) as failure:
        execute_finetune_runtime_provisioning(plan, allow_downloads=False)
    assert failure.value.code == "FINETUNE.DOWNLOADS_NOT_ALLOWED"


def test_download_is_verified_against_its_pin_and_partials_never_survive(tmp_path: Path) -> None:
    payload = b"weights" * 100
    item = FineTuneDownload(
        url="https://example.invalid/w.st",
        destination=tmp_path / "pretrained" / "w.st",
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    assert download_verified(item, opener=lambda _url: io.BytesIO(payload)) == item.destination
    assert item.present
    wrong = FineTuneDownload(
        url=item.url,
        destination=tmp_path / "pretrained" / "bad.st",
        sha256=item.sha256,
        size_bytes=99,
    )
    with pytest.raises(FineTuneError) as failure:
        download_verified(wrong, opener=lambda _url: io.BytesIO(payload))
    assert failure.value.code == "FINETUNE.PRETRAINED_MISMATCH"
    assert not wrong.destination.exists() and not list((tmp_path / "pretrained").glob(".*partial"))


def test_fetch_pretrained_keeps_matching_files_and_refuses_silent_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cole_voice_lab.finetune as finetune_module

    served = {
        "model_1250000.safetensors": b"w" * 10,
        "vocab.txt": b"v" * 4,
        "config.yaml": b"c" * 3,
        "pytorch_model.bin": b"m" * 7,
    }
    for prefix, name in (
        ("FINETUNE_PRETRAINED", "model_1250000.safetensors"),
        ("FINETUNE_PRETRAINED_VOCAB", "vocab.txt"),
        ("FINETUNE_VOCODER_CONFIG", "config.yaml"),
        ("FINETUNE_VOCODER_MODEL", "pytorch_model.bin"),
    ):
        payload = served[name]
        monkeypatch.setattr(
            finetune_module, f"{prefix}_SHA256", hashlib.sha256(payload).hexdigest()
        )
        monkeypatch.setattr(finetune_module, f"{prefix}_SIZE_BYTES", len(payload))
    root = tmp_path / "rt"
    with pytest.raises(FineTuneError) as refused:
        fetch_finetune_pretrained(root, allow_downloads=False)
    assert refused.value.code == "FINETUNE.DOWNLOADS_NOT_ALLOWED"
    assert "vocos-mel-24khz/pytorch_model.bin" in str(refused.value)
    calls: list[str] = []

    def opener(url: str) -> io.BytesIO:
        calls.append(url)
        return io.BytesIO(served[url.rsplit("/", 1)[1]])

    paths = fetch_finetune_pretrained(root, allow_downloads=True, opener=opener)
    assert [path.relative_to(root.resolve()).as_posix() for path in paths] == [
        "pretrained/F5TTS_v1_Base/model_1250000.safetensors",
        "pretrained/F5TTS_v1_Base/vocab.txt",
        "pretrained/vocos-mel-24khz/config.yaml",
        "pretrained/vocos-mel-24khz/pytorch_model.bin",
    ]
    assert len(calls) == 4 and paths[3].read_bytes() == served["pytorch_model.bin"]
    assert all(
        "charactr/vocos-mel-24khz/resolve/0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21/" in url
        for url in calls[2:]
    )
    fetch_finetune_pretrained(root, allow_downloads=False)  # already present: no download needed
    assert len(calls) == 4


def test_unreadable_library_is_a_named_projection_failure(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    (layout.root / "tmp").chmod(0o000)
    try:
        with pytest.raises(FineTuneError) as failure:
            select_finetune_corpus(layout, released_pilot_v2(), SUBJECT)
        assert failure.value.code == "FINETUNE.CORPUS_PROJECTION_FAILED"
    finally:
        (layout.root / "tmp").chmod(0o700)


def test_runtime_subprocess_gets_a_home_and_never_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import pwd

    root, _pin = _stub_runtime(tmp_path / "runtime")
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    output = finetune_module._run_runtime(
        root / "bin" / "python",
        ["-c", finetune_module._RUNTIME_PROBE],
        cwd=None,
        log=None,
        what="environment probe",
    )
    described = json.loads(output.strip().splitlines()[-1])
    assert described["home"] == pwd.getpwuid(os.getuid()).pw_dir
    assert described["hf_hub_offline"] == "1"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert finetune_module._runtime_environment()["HOME"] == str(tmp_path / "home")


def test_an_incomplete_corpus_is_refused_by_name_unless_missing_prompts_are_allowed() -> None:
    complete = _corpus_with_one_entry()
    require_complete_corpus(complete, allow_missing=False)
    missing = _corpus_with_one_entry(
        missing_prompt_ids=tuple(f"missing-{number}" for number in range(400))
    )
    unknown = _corpus_with_one_entry(unknown_record_ids=("record-x",))
    for corpus in (missing, unknown):
        require_complete_corpus(corpus, allow_missing=True)
        with pytest.raises(FineTuneError) as refused:
            require_complete_corpus(corpus, allow_missing=False)
        assert refused.value.code == "FINETUNE.CORPUS_INCOMPLETE"
    with pytest.raises(FineTuneError) as refused:
        require_complete_corpus(missing, allow_missing=False)
    message = str(refused.value)
    # The counts and the advice lead, so a 1,024-character wire copy keeps them.
    assert message[:1024].startswith(
        "the corpus is incomplete: 400 of 401 spoken prompts have no kept take and 0 records "
        "could not be read; record the missing prompts or allow missing prompts (--allow-missing)"
    )
    assert "missing-399" in message


def test_cli_run_refuses_an_incomplete_corpus_by_code_before_touching_the_runtime(
    native_coverage: NativeCoverage, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    layout = native_coverage.rig.layout
    command = ["finetune", "run", "--asset-root", str(layout.root), "--catalog", "pilot-v2"]
    status = main([*command, "--runtime-root", str(tmp_path / "no-runtime"), "--run-id", "r"])
    assert status == 2
    error = capsys.readouterr().err
    assert error.startswith("voice-lab: FINETUNE.CORPUS_INCOMPLETE: the corpus is incomplete")
    assert not (layout.root / "derived/finetune/r").exists()


@pytest.mark.parametrize("state", ["queued", "running"])
def test_cli_training_refuses_while_the_apps_job_is_active(
    native_coverage: NativeCoverage,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    from cole_voice_lab.recording_contracts import FineTuneCommand
    from cole_voice_lab.recording_finetune_jobs import FineTuneJobQueue, active_finetune_job

    layout = native_coverage.rig.layout
    assert active_finetune_job(layout) is None  # no queue directory yet
    queue = FineTuneJobQueue(
        layout,
        subject_id=SUBJECT,
        runtime_root=tmp_path / "runtime",
        free_bytes=lambda _path: 1 << 40,
    )
    try:
        assert active_finetune_job(layout) is None  # an empty head
        (tmp_path / "runtime" / "bin").mkdir(parents=True)
        (tmp_path / "runtime" / "bin" / "python").write_text("")
        queue.submit(FineTuneCommand(operation_id="app-job", allow_missing=True))
        if state == "running":
            assert queue.claim() is not None
    finally:
        queue.close()
    job = active_finetune_job(layout)
    assert job is not None and job.state == state
    before = sorted(path.name for path in (layout.root / "provenance/finetune-jobs").iterdir())

    def touched(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the runtime was used while the app's job was active")

    monkeypatch.setattr(finetune_module, "describe_finetune_runtime", touched)
    runtime = ["--runtime-root", str(tmp_path / "runtime")]
    base = ["--asset-root", str(layout.root)]
    for command in (
        ["finetune", "run", *base, "--catalog", "pilot-v2", "--allow-missing", *runtime],
        ["finetune", "train", *base, "--run-id", "r", *runtime],
        ["finetune", "synthesize", *base, "--run-id", "r", *runtime],
    ):
        assert main(command) == 2
        error = capsys.readouterr().err
        assert error.startswith(
            f"voice-lab: FINETUNE.JOB_ACTIVE: the recording app's fine-tune app-job is {state}"
        )
    assert not (layout.root / "derived/finetune").exists()
    after = sorted(path.name for path in (layout.root / "provenance/finetune-jobs").iterdir())
    assert after == before  # read only


def test_cli_gpu_work_refuses_while_the_app_speaks(
    native_coverage: NativeCoverage,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess
    import sys

    from cole_voice_lab.errors import SecurityError
    from cole_voice_lab.recording_finetune_jobs import (
        SPEECH_ACTIVE_MARKER,
        FineTuneJobQueue,
        SpeechActiveMarker,
        active_speech_request,
    )
    from cole_voice_lab.recording_takes import write_recording_evidence

    layout = native_coverage.rig.layout
    assert active_speech_request(layout) is None  # no queue directory yet
    FineTuneJobQueue(layout, subject_id=SUBJECT, runtime_root=None).close()
    marker = layout.root / SPEECH_ACTIVE_MARKER
    live = SpeechActiveMarker(operation_id="speak-7", pid=os.getpid(), started_at=START)
    write_recording_evidence(marker, live)
    assert active_speech_request(layout) == live

    def reached(*args: Any, **kwargs: Any) -> None:
        raise FineTuneError("FINETUNE.RUNTIME_UNAVAILABLE", "synthetic: the runtime was reached")

    monkeypatch.setattr(finetune_module, "describe_finetune_runtime", reached)
    runtime = ["--runtime-root", str(tmp_path / "runtime")]
    base = ["--asset-root", str(layout.root)]
    commands = (
        ["finetune", "run", *base, "--catalog", "pilot-v2", "--allow-missing", *runtime],
        ["finetune", "train", *base, "--run-id", "r", *runtime],
        ["finetune", "synthesize", *base, "--run-id", "r", *runtime],
    )
    for command in commands:
        assert main(command) == 2
        assert capsys.readouterr().err.startswith(
            "voice-lab: FINETUNE.JOB_ACTIVE: the recording app's Speak request speak-7 is running"
        )
    assert marker.read_bytes() == canonical_json_bytes(live) + b"\n"  # read only
    # A marker whose service is gone refuses nothing.
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    write_recording_evidence(marker, live.model_copy(update={"pid": gone.pid}))
    assert active_speech_request(layout) is None
    for command in commands:
        assert main(command) == 2
        assert "FINETUNE.JOB_ACTIVE" not in capsys.readouterr().err
    # A damaged marker is refused by name, never ignored.
    marker.chmod(0o644)
    with pytest.raises(SecurityError):
        active_speech_request(layout)
    assert main(commands[2]) == 2
    assert "recording evidence must be one private regular file" in capsys.readouterr().err


def test_runtime_link_is_released_only_when_it_points_at_this_run(tmp_path: Path) -> None:
    checkpoints = tmp_path / "run" / "checkpoints"
    other = tmp_path / "other" / "checkpoints"
    checkpoints.mkdir(parents=True)
    other.mkdir(parents=True)
    ckpts = tmp_path / "ckpts"
    ckpts.mkdir()
    link = ckpts / "run"
    assert release_finetune_runtime_link(link, checkpoints) is False  # absent
    link.mkdir()
    assert release_finetune_runtime_link(link, checkpoints) is False  # not a link
    link.rmdir()
    os.symlink(other.resolve(), link)
    assert release_finetune_runtime_link(link, checkpoints) is False  # another run's
    assert link.is_symlink()
    link.unlink()
    os.symlink(checkpoints.resolve(), link)
    assert release_finetune_runtime_link(link, checkpoints) is True
    assert not os.path.lexists(link) and checkpoints.is_dir()


def _write_run(
    layout: AssetLayout,
    run_id: str,
    *,
    status: str = "finished",
    started_minute: int = 0,
    finished_minute: int | None = 10,
    samples: bool = True,
    error: str | None = None,
) -> FineTuneRun:
    """A synthetic run directory: run.json bound to dataset/{dataset,corpus}.json."""
    run_root = f"derived/finetune/{run_id}"
    directory = layout.root / run_root
    (directory / "dataset").mkdir(parents=True, mode=0o700)
    corpus = _corpus_with_one_entry()
    dataset = FineTuneDataset(
        corpus_sha256=canonical_sha256(corpus),
        dataset_root=f"{run_root}/dataset",
        normalization_rule="synthetic",
        clips=(
            FineTuneDatasetClip(
                prompt_id=corpus.entries[0].prompt_id,
                take_id="take-x",
                relative_path=f"wavs/{corpus.entries[0].prompt_id}.wav",
                sha256="3" * 64,
                frames=240,
                gain_db=0.0,
                text=corpus.entries[0].text,
            ),
        ),
        metadata_csv_sha256="4" * 64,
        materialized_at=START,
    )
    write_canonical_json(directory / "dataset" / "corpus.json", corpus)
    write_canonical_json(directory / "dataset" / "dataset.json", dataset)
    run = FineTuneRun(
        run_id=run_id,
        subject_id=SUBJECT,
        catalog_sha256=corpus.catalog_sha256,
        corpus_sha256=canonical_sha256(corpus),
        dataset_sha256=canonical_sha256(dataset),
        hyperparameters=FineTuneHyperparameters(epochs=3),
        runtime=FineTuneRuntimeIdentity(
            runtime_root="/synthetic/runtime",
            python_sha256="5" * 64,
            f5_tts_version="stub",
            torch_version="stub",
            pretrained_path="/synthetic/runtime/pretrained/model.safetensors",
            pretrained_sha256="6" * 64,
        ),
        run_root=run_root,
        log_path=f"{run_root}/train.log",
        checkpoints=(),
        status=status,
        started_at=START + timedelta(minutes=started_minute),
        finished_at=None if finished_minute is None else START + timedelta(minutes=finished_minute),
        error=error,
    )
    write_canonical_json(directory / "run.json", run)
    if samples:
        for label in ("finetuned", "base"):
            sample_directory = directory / "samples" / label
            sample_directory.mkdir(parents=True)
            sample_set = FineTuneSampleSet(
                label=label,
                checkpoint_path="/synthetic/checkpoint.pt",
                checkpoint_sha256="7" * 64,
                use_ema=True,
                nfe_steps=32,
                device="mps",
                reference_prompt_id=corpus.entries[0].prompt_id,
                reference_audio_sha256="3" * 64,
                samples=(
                    FineTuneSample(
                        index=1,
                        relative_path=f"{run_root}/samples/{label}/sample-01.wav",
                        sha256="8" * 64,
                        seconds=0.5,
                        text="One.",
                    ),
                ),
                created_at=START,
            )
            write_canonical_json(sample_directory / "samples.json", sample_set)
    return run


def test_latest_finished_run_is_the_newest_finished_record(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    assert latest_finished_finetune_run(layout) is None  # no derived/finetune yet
    (layout.root / "derived/finetune").mkdir()
    assert latest_finished_finetune_run(layout) is None
    _write_run(layout, "finetune-a", finished_minute=10)
    _write_run(layout, "finetune-b", finished_minute=30, samples=False)
    _write_run(layout, "finetune-c", finished_minute=30)  # same time: the greater id wins
    _write_run(layout, "finetune-d", status="running", finished_minute=None)
    _write_run(layout, "finetune-e", status="failed", finished_minute=50, error="stopped")
    # Not runs: a dataset-only directory, a file, a symlink and a name that is no stable id.
    (layout.root / "derived/finetune/dataset-only/dataset").mkdir(parents=True)
    (layout.root / "derived/finetune/notes.txt").write_text("synthetic")
    os.symlink(layout.root / "derived/finetune/finetune-a", layout.root / "derived/finetune/z")
    (layout.root / "derived/finetune/Not A Run").mkdir()
    latest = latest_finished_finetune_run(layout)
    assert latest is not None
    assert (latest.run_id, latest.status, latest.epochs) == ("finetune-c", "finished", 3)
    assert latest.prompt_count == 1 and latest.retained_seconds == 0.01
    assert latest.finetuned_sample_paths == (
        "derived/finetune/finetune-c/samples/finetuned/sample-01.wav",
    )
    assert latest.base_sample_paths == ("derived/finetune/finetune-c/samples/base/sample-01.wav",)
    unsampled = finetune_run_summary(layout, "derived/finetune/finetune-b")
    assert unsampled.finetuned_sample_paths == () and unsampled.base_sample_paths == ()
    failed = finetune_run_summary(layout, "derived/finetune/finetune-e")
    assert failed.status == "failed" and failed.error == "stopped"
    assert failed.finetuned_sample_paths == ()


@pytest.mark.parametrize("damage", ["corrupt", "renamed", "unbound", "missing-corpus"])
def test_a_damaged_run_record_is_a_named_failure(tmp_path: Path, damage: str) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    run = _write_run(layout, "finetune-a")
    directory = layout.root / run.run_root
    if damage == "corrupt":
        (directory / "run.json").write_text("{not json")
    elif damage == "renamed":
        write_canonical_json(directory / "run.json", run.model_copy(update={"run_id": "other"}))
    elif damage == "unbound":
        write_canonical_json(
            directory / "run.json", run.model_copy(update={"dataset_sha256": "9" * 64})
        )
    else:
        (directory / "dataset" / "corpus.json").unlink()
    with pytest.raises(FineTuneError) as failure:
        latest_finished_finetune_run(layout)
    assert failure.value.code == "FINETUNE.SOURCE_CHANGED"


def test_a_summary_that_breaks_its_contract_is_a_named_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    run = _write_run(layout, "finetune-a")
    monkeypatch.setattr(finetune_module, "_MAX_SUMMARY_SAMPLES", 0)
    summary = finetune_run_summary(layout, run.run_root)
    assert summary.finetuned_sample_paths == ()
    inverted = run.model_copy(
        update={"started_at": START + timedelta(minutes=20)}  # finishes before it started
    )
    write_canonical_json(layout.root / run.run_root / "run.json", inverted)
    with pytest.raises(FineTuneError, match="cannot be summarized") as failure:
        finetune_run_summary(layout, run.run_root)
    assert failure.value.code == "FINETUNE.SOURCE_CHANGED"


def test_an_oversized_run_record_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    run = _write_run(layout, "finetune-a")
    monkeypatch.setattr(finetune_module, "MAX_FINETUNE_RECORD_BYTES", 16)
    with pytest.raises(FineTuneError, match="larger than 16 bytes"):
        finetune_run_summary(layout, run.run_root)


def test_a_stopped_run_is_rewritten_failed_only_while_it_says_running(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    assert mark_finetune_run_failed(layout, "derived/finetune/absent", "x") is None
    running = _write_run(layout, "finetune-a", status="running", finished_minute=None)
    checkpoints = layout.root / running.run_root / "checkpoints"
    checkpoints.mkdir()
    (checkpoints / "model_540.pt").write_bytes(b"intermediate")
    rewritten = mark_finetune_run_failed(
        layout, running.run_root, "cancelled by the operator", clock=lambda: START
    )
    assert rewritten is not None and rewritten.status == "failed"
    assert rewritten.error == "cancelled by the operator" and rewritten.finished_at == START
    assert [(item.kind, item.update) for item in rewritten.checkpoints] == [("intermediate", 540)]
    stored = FineTuneRun.model_validate_json(
        (layout.root / running.run_root / "run.json").read_bytes()
    )
    assert stored == rewritten
    finished = _write_run(layout, "finetune-b")
    assert mark_finetune_run_failed(layout, finished.run_root, "late") == finished
    assert (
        FineTuneRun.model_validate_json(
            (layout.root / finished.run_root / "run.json").read_bytes()
        ).status
        == "finished"
    )


@needs_ffmpeg
def test_pipeline_reports_its_stages_and_releases_the_link_when_training_fails(
    native_coverage: NativeCoverage, tmp_path: Path
) -> None:
    layout = native_coverage.rig.layout
    root, pin = _stub_runtime(tmp_path / "runtime", {"fail_training": True})
    steps: list[FineTunePipelineStep] = []
    with pytest.raises(FineTuneError) as failure:
        run_finetune_pipeline(
            layout,
            native_coverage.pilot_v2,
            SUBJECT,
            root,
            FineTuneHyperparameters(epochs=1),
            run_id="stages",
            allow_missing=True,
            pretrained_pin=pin,
            progress=steps.append,
        )
    assert failure.value.code == "FINETUNE.TRAINING_FAILED"
    link = root / "lib" / "python3.12" / "ckpts" / "stages"
    assert [step.stage for step in steps] == ["selecting", "dataset", "training"]
    assert steps[0].runtime_link is None
    assert all(step.runtime_link == link.resolve() for step in steps[1:])
    assert all(step.run_id == "stages" for step in steps)
    assert not os.path.lexists(link)
    record = FineTuneRun.model_validate_json(
        (layout.root / "derived/finetune/stages/run.json").read_bytes()
    )
    assert record.status == "failed"
