"""Independently verify canonical evidence produced by the synthetic native review harness."""

from __future__ import annotations

import json
import wave
from pathlib import Path

from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_edits import RecordingEditDerivative, RecordingEditRecipe
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_review import RecordingReviewResolver
from cole_voice_lab.recording_takes import RecordingTakeStore


def _pcm(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as source:
        assert (
            source.getnchannels(),
            source.getsampwidth(),
            source.getframerate(),
        ) == (1, 3, 48000)
        frames = source.getnframes()
        assert 0 < frames <= 4801
        return frames, source.readframes(frames)


def verify(directory: Path, *, legacy: bool) -> None:
    layout = AssetLayout.validate(directory / "assets")
    report = json.loads(
        (
            directory / "native" / ("legacy-review-report.json" if legacy else "review-report.json")
        ).read_text()
    )
    store = RecordingTakeStore(layout)
    cursor = store.get_cursor("synthetic-subject")
    assert cursor is not None and cursor.selection.take_id == report["take_id"]
    assert cursor.selection.prompt_id == "reading-one"
    if legacy:
        with RecordingIndex(layout, subject_id="synthetic-subject") as index:
            source = RecordingReviewResolver(layout, store, index).resolve(
                cursor.selection.catalog_sha256, report["take_id"]
            )
        assert source.original_kind == report["original_kind"]
        assert source.decision == "kept" and source.edit_revision == 2
        assert (source.frame_range.start_frame, source.frame_range.end_frame) == (
            0,
            960,
        )
        original = layout.root / source.source_path
        assert sha256_file(original)[0] == report["source_sha256"]
        _, pcm = _pcm(original)
        derivatives = list((layout.root / "derived/recording-edits").rglob("*.wav"))
        assert derivatives
        assert any(_pcm(path) == (296, pcm[17 * 3 : 313 * 3]) for path in derivatives)
        evidence = {
            "original_kind": source.original_kind,
            "original_sha256": source.source_sha256,
            "exact_legacy_trim_frames": 296,
            "final_edit_revision": source.edit_revision,
        }
    else:
        take = store.get_take(report["take_id"])
        assert take.seal is not None and take.frame_range is not None
        assert take.decision == "kept" and take.decision_revision == 5 and take.edit_revision == 2
        assert (take.frame_range.start_frame, take.frame_range.end_frame) == (0, 4801)
        original = layout.root / take.seal.original_path
        assert sha256_file(original)[0] == report["source_sha256"] == take.seal.original_sha256
        frames, pcm = _pcm(original)
        pattern = bytes([0, 0, 0, 255, 255, 127, 0, 0, 128])
        assert frames == 4801 and pcm == (pattern * 1601)[: 4801 * 3]
        recipe_root = layout.root / "provenance/recording-edits/native" / report["take_id"]
        recipes = [
            RecordingEditRecipe.model_validate_json(path.read_bytes())
            for path in recipe_root.glob("*.json")
            if len(path.stem) == 64
        ]
        assert sorted(recipe.edit_revision for recipe in recipes) == [1, 2]
        for recipe in recipes:
            assert (recipe_root / f"{canonical_sha256(recipe)}.json").is_file()
        receipt_path = next(recipe_root.glob("*.derivative.json"))
        derivative = RecordingEditDerivative.model_validate_json(receipt_path.read_bytes())
        assert (
            derivative.edit_revision == 1
            and derivative.frame_range.start_frame == 100
            and derivative.frame_range.end_frame == 2100
        )
        assert derivative.source_sha256 == take.seal.original_sha256
        derived = layout.root / derivative.derived_path
        assert sha256_file(derived) == (
            derivative.derived_sha256,
            derivative.derived_size_bytes,
        )
        assert _pcm(derived) == (2000, pcm[100 * 3 : 2100 * 3])
        evidence = {
            "original_sha256": take.seal.original_sha256,
            "source_frames": frames,
            "exact_native_trim_frames": 2000,
            "derived_sha256": derivative.derived_sha256,
            "canonical_recipe_sha256": derivative.recipe_sha256,
            "final_edit_revision": take.edit_revision,
            "final_decision_revision": take.decision_revision,
        }
    write_canonical_json(directory / "native/python-review-evidence.json", evidence)
