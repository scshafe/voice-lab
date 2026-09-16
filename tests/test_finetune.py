"""The fine-tuning corpus is the latest retained take per spoken prompt (finetune.py)."""

from __future__ import annotations

import hashlib
import io
import json
import math
import wave
from array import array
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
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

from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256, sha256_file
from cole_voice_lab.cli import main
from cole_voice_lab.corpus_store import CorpusSnapshotSecurityError, open_corpus_store
from cole_voice_lab.finetune import (
    FineTuneCorpus,
    FineTuneCorpusEntry,
    FineTuneDataset,
    FineTuneDownload,
    FineTuneError,
    FineTuneHyperparameters,
    FineTuneRun,
    FineTuneSampleSet,
    describe_finetune_runtime,
    download_verified,
    execute_finetune_runtime_provisioning,
    fetch_finetune_pretrained,
    finetune_cli_arguments,
    materialize_finetune_dataset,
    normalization_gain_db,
    plan_finetune_runtime_provisioning,
    run_f5_finetune,
    select_finetune_corpus,
    synthesize_finetune_samples,
)
from cole_voice_lab.models import PromptSuite, utc_now
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_analysis import RecordingAnalysisQueue
from cole_voice_lab.recording_contracts import (
    CatalogReference,
    CatalogSnapshot,
    RecordingFrameRange,
    catalog_snapshot_path,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_takes import RecordingTakeStore

SPOKEN_PROMPT_COUNT = 34


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
    with open_corpus_store(layout, create=False) as corpus:
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
    with open_corpus_store(layout, create=False) as corpus:
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


STUB_RUNTIME_PYTHON = '''#!{python}
"""Stand-in for the F5 runtime python: answers the probe, packs, trains and synthesizes."""
import json, os, sys, wave
from pathlib import Path

ROOT = Path({root!r})
PACKAGE = ROOT / "lib" / "python3.12" / "site-packages" / "f5_tts"
argv = [a for a in sys.argv[1:] if a != "-B"]
if argv[:1] == ["-c"]:
    print(json.dumps({{"f5_tts_version": "1.1.22-stub", "torch_version": "2.14.0-stub",
                       "package_path": str(PACKAGE)}}))
    sys.exit(0)
script = Path(argv[0]).name
if script == "prepare_csv_wavs.py":
    out = Path(argv[2]); out.mkdir(parents=True, exist_ok=True)
    rows = Path(argv[1]).read_text().splitlines()[1:]
    (out / "raw.arrow").write_bytes(b"arrow")
    (out / "duration.json").write_text(json.dumps({{"duration": [1.0] * len(rows)}}))
    print(f"packed {{len(rows)}} rows")
    sys.exit(0)
if script == "f5_finetune_launcher.py":
    name = argv[argv.index("--dataset_name") + 1]
    print("lab launcher stub argv: " + " ".join(argv[1:]))
    if name.startswith("fail"):
        print("Traceback: stub training failure", file=sys.stderr); sys.exit(1)
    ckpts = ROOT / "lib" / "python3.12" / "ckpts" / name
    (ckpts / "model_9.pt").write_bytes(b"intermediate")
    (ckpts / "model_last.pt").write_bytes(b"last")
    print("Saved last checkpoint at update 9")
    sys.exit(0)
if script == "f5_synthesize.py":
    job = json.loads(Path(argv[1]).read_text())
    out = Path(job["output_directory"]); out.mkdir(parents=True, exist_ok=True)
    assert Path(job["checkpoint_path"]).is_file() and Path(job["reference_audio_path"]).is_file()
    for index, text in enumerate(job["texts"], start=1):
        path = out / f"sample-{{index:02d}}.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000)
            handle.writeframes(bytes(480))
        print(json.dumps({{"index": index, "path": str(path), "seconds": 0.01}}))
    sys.exit(0)
print("unexpected invocation", argv, file=sys.stderr); sys.exit(3)
'''


def _stub_runtime(root: Path) -> tuple[Path, tuple[str, int]]:
    """A fake F5 runtime whose python is a script; returns its root and the fake pretrained pin."""
    import sys

    (root / "bin").mkdir(parents=True)
    python = root / "bin" / "python"
    python.write_text(STUB_RUNTIME_PYTHON.format(python=sys.executable, root=str(root)))
    python.chmod(0o700)
    package = root / "lib" / "python3.12" / "site-packages" / "f5_tts"
    (package / "train" / "datasets").mkdir(parents=True)
    (package / "train" / "datasets" / "prepare_csv_wavs.py").write_text("# stub\n")
    pretrained = root / "pretrained" / "F5TTS_v1_Base"
    pretrained.mkdir(parents=True)
    (pretrained / "model_1250000.safetensors").write_bytes(b"pretrained-weights")
    (pretrained / "vocab.txt").write_text("a\nb\n")
    digest, size = sha256_file(pretrained / "model_1250000.safetensors")
    return root, (digest, size)


def test_describe_runtime_refuses_wrong_pretrained_weights(tmp_path: Path) -> None:
    root, pin = _stub_runtime(tmp_path / "runtime")
    with pytest.raises(FineTuneError) as failure:
        describe_finetune_runtime(root)  # the real pin, which the stub cannot match
    assert failure.value.code == "FINETUNE.PRETRAINED_MISMATCH"
    runtime = describe_finetune_runtime(root, pretrained_pin=pin)
    assert runtime.identity.f5_tts_version == "1.1.22-stub"
    assert runtime.data_directory == (root / "lib" / "python3.12" / "data").resolve()
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
    assert (
        samples.checkpoint_sha256 == pin[0]
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
    import cole_voice_lab.finetune as finetune_module

    root, pin = _stub_runtime(tmp_path / "runtime")
    monkeypatch.setattr(finetune_module, "FINETUNE_PRETRAINED_SHA256", pin[0])
    monkeypatch.setattr(finetune_module, "FINETUNE_PRETRAINED_SIZE_BYTES", pin[1])
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
    assert urls == [
        "https://huggingface.co/SWivid/F5-TTS/resolve/84e5a410d9cead4de2f847e7c9369a6440bdfaca/"
        "F5TTS_v1_Base/model_1250000.safetensors",
        "https://huggingface.co/SWivid/F5-TTS/resolve/84e5a410d9cead4de2f847e7c9369a6440bdfaca/"
        "F5TTS_v1_Base/vocab.txt",
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

    weights, vocab = b"w" * 10, b"v" * 4
    monkeypatch.setattr(
        finetune_module, "FINETUNE_PRETRAINED_SHA256", hashlib.sha256(weights).hexdigest()
    )
    monkeypatch.setattr(finetune_module, "FINETUNE_PRETRAINED_SIZE_BYTES", len(weights))
    monkeypatch.setattr(
        finetune_module, "FINETUNE_PRETRAINED_VOCAB_SHA256", hashlib.sha256(vocab).hexdigest()
    )
    monkeypatch.setattr(finetune_module, "FINETUNE_PRETRAINED_VOCAB_SIZE_BYTES", len(vocab))
    root = tmp_path / "rt"
    with pytest.raises(FineTuneError) as refused:
        fetch_finetune_pretrained(root, allow_downloads=False)
    assert refused.value.code == "FINETUNE.DOWNLOADS_NOT_ALLOWED"
    served = {"model_1250000.safetensors": weights, "vocab.txt": vocab}
    calls: list[str] = []

    def opener(url: str) -> io.BytesIO:
        calls.append(url)
        return io.BytesIO(served[url.rsplit("/", 1)[1]])

    paths = fetch_finetune_pretrained(root, allow_downloads=True, opener=opener)
    assert [path.name for path in paths] == ["model_1250000.safetensors", "vocab.txt"]
    assert len(calls) == 2 and paths[0].read_bytes() == weights
    fetch_finetune_pretrained(root, allow_downloads=False)  # already present: no download needed
    assert len(calls) == 2


def test_unreadable_library_is_a_named_projection_failure(tmp_path: Path) -> None:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    (layout.root / "tmp").chmod(0o000)
    try:
        with pytest.raises(FineTuneError) as failure:
            select_finetune_corpus(layout, released_pilot_v2(), SUBJECT)
        assert failure.value.code == "FINETUNE.CORPUS_PROJECTION_FAILED"
    finally:
        (layout.root / "tmp").chmod(0o700)
