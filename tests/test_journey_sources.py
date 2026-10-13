"""The recording index's source port (Recording v1 step 7, D-051): the two adapters list
exactly what the listers listed, every row says its source, a third source plugs in
without touching the index, and the stages after the index treat Voice Memos rows as
they did when those rows had no ``source``. Synthetic metadata only."""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from journey_browser_fixtures import fake_results
from test_journey_corpus import measurement_manifest, raw_package

from cole_voice_lab.jobs.watch import Watchd, WatchOptions
from cole_voice_lab.journey import features, index, jsonio
from cole_voice_lab.journey import filter as vj_filter
from cole_voice_lab.journey.browser import (
    build_corpus_rows,
    filter_rows,
    open_recording_audio,
    project_rows,
)
from cole_voice_lab.journey.corpus import (
    Entry,
    list_corpus_entries,
    list_measurement_entries,
    recording_id,
)
from cole_voice_lab.journey.paths import resolve_paths
from cole_voice_lab.journey.sources import (
    MEASUREMENT,
    VOICE_MEMOS,
    MeasurementSource,
    RecordingSource,
    SourceError,
    VoiceMemosSource,
    configured_sources,
    list_source_entries,
    source_type,
)
from cole_voice_lab.journey.stt import SttOptions, transcribe_index_manifest

MEMO_A = "20190301 090000-AAAA1111.m4a"
MEMO_B = "20200415 181500-BBBB2222.m4a"
MEMO_C = "20210102 070000-CCCC3333.m4a"
MEMO_HELD = "20220202 020202-DDDD4444.m4a"
MEMO_ODD = "Voice note.m4a"
GENERATED_AT = "2026-10-12T00:00:00.000Z"
SINGING = "singing-studio"


def phone_drop(root: Path) -> Path:
    """A mirrored drop: three listed memos, one the manifest has not caught up with, one
    held back (its size differs from the manifest's), and the files a drop also holds."""

    phone = root / "phone"
    (phone / "_mirror").mkdir(parents=True)
    (phone / ".rsync-partial").mkdir()
    for name, body in {
        MEMO_A: "aaa",
        MEMO_B: "bbbb",
        MEMO_C: "c",
        MEMO_HELD: "held",
        MEMO_ODD: "odd",
    }.items():
        (phone / name).write_text(body)
    (phone / f".{MEMO_A}.Ab12Cd").write_text("temp")
    (phone / ".rsync-partial" / MEMO_A).write_text("partial")
    (phone / "_mirror" / "stray.m4a").write_text("not a recording")
    (phone / "CloudRecordings.db").write_text("db")
    listed = [(MEMO_A, 3), (MEMO_B, 4), (MEMO_HELD, 99), (MEMO_ODD, 3)]
    (phone / "_mirror" / "latest.json").write_text(
        json.dumps(
            {
                "schema": "voice-journey-mirror-manifest/1",
                "runId": "r1",
                "files": [{"path": name, "sizeBytes": size} for name, size in listed],
            }
        )
    )
    return phone


def measurement_root(root: Path) -> Path:
    """voice-journey's own session directory and an accepted ``raw/`` package."""

    measurement = root / "measurement"
    session = measurement / "sessionA1"
    session.mkdir(parents=True)
    (session / "seg-1.wav").write_text("w")
    (session / "seg-2.wav").write_text("ww")
    (session / "session.json").write_text(
        json.dumps(
            {
                "chainId": "umik1",
                "startedAt": "2024-01-01T11:00:00.000Z",
                "files": [{"name": "seg-1.wav"}, {"name": "seg-2.wav"}],
                "segments": [{"file": "seg-1.wav", "key": "vowel-a"}],
            }
        )
    )
    raw_package(measurement, "a" * 64, measurement_manifest())
    return measurement


def untagged(entry: Entry) -> Entry:
    return {key: value for key, value in entry.items() if key != "source"}


@dataclass(frozen=True)
class FakeSingingSource:
    """A third source as the singing studio would add one: its own type, its own id
    namespace, the listing's shape."""

    takes: Sequence[tuple[str, str]]

    @property
    def type(self) -> str:
        return SINGING

    def list_entries(self) -> list[Entry]:
        return [
            {
                "recordingId": recording_id(f"{SINGING}/{name}"),
                "filename": name,
                "capturedAt": captured_at,
                "sizeBytes": 7,
                "modifiedAt": "2026-10-12T00:00:00.000Z",
                "source": {"type": SINGING, "studioTakeId": f"take-{index}"},
            }
            for index, (name, captured_at) in enumerate(self.takes)
        ]


@dataclass(frozen=True)
class Listed:
    """A source that lists what it is given (for the contract's refusals)."""

    kind: Any
    entries: Sequence[Entry] = ()

    @property
    def type(self) -> Any:
        return self.kind

    def list_entries(self) -> list[Entry]:
        return [dict(entry) for entry in self.entries]


def run_index(
    tmp_path: Path,
    out: Path,
    *extra: str,
    sources: Any = index.index_sources,
) -> dict[str, Any]:
    args = [
        "--corpus-root",
        str(tmp_path / "phone"),
        "--measurement-root",
        str(tmp_path / "measurement"),
        "--out",
        str(out),
        "--generated-at",
        GENERATED_AT,
        *extra,
    ]
    assert index.run(args, sources=sources) == 0
    manifest: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    return manifest


def legacy_index(tmp_path: Path, out: Path, previous: Any = None) -> str:
    """The index as it was built before the port (the listers called directly), which
    the parity harness found identical to Node's: the bytes it would have written."""

    phone = list_corpus_entries(str(tmp_path / "phone"), mirror_manifest=True)
    recordings = [
        *index.mark_phone_state(phone, previous),
        *list_measurement_entries(str(tmp_path / "measurement")),
    ]
    manifest = index.build_manifest(recordings, {"generatedAt": GENERATED_AT})
    return jsonio.stringify(manifest) + "\n"


def strip_phone_sources(manifest: dict[str, Any]) -> dict[str, Any]:
    stripped = copy.deepcopy(manifest)
    for row in stripped["recordings"]:
        if row.get("source") == {"type": VOICE_MEMOS}:
            del row["source"]
    return stripped


# ---- the adapters --------------------------------------------------------------------


def test_the_voice_memos_adapter_lists_the_drop_exactly_and_tags_each_entry(
    tmp_path: Path,
) -> None:
    phone = phone_drop(tmp_path)
    before = list_corpus_entries(str(phone), mirror_manifest=True)
    entries = VoiceMemosSource(str(phone)).list_entries()
    assert [untagged(entry) for entry in entries] == before
    assert [entry["filename"] for entry in entries] == [MEMO_A, MEMO_B, MEMO_C, MEMO_ODD]
    assert all(entry["source"] == {"type": "voice-memos"} for entry in entries)
    assert list(entries[0]) == [
        "recordingId",
        "filename",
        "capturedAt",
        "sizeBytes",
        "modifiedAt",
        "source",
        "inManifest",
    ]
    assert [untagged(entry) for entry in VoiceMemosSource(str(phone), limit=2).list_entries()] == (
        list_corpus_entries(str(phone), limit=2, mirror_manifest=True)
    )
    assert VoiceMemosSource(str(phone)).type == VOICE_MEMOS == "voice-memos"


def test_the_measurement_adapter_lists_the_sessions_exactly(tmp_path: Path) -> None:
    root = measurement_root(tmp_path)
    entries = MeasurementSource(str(root)).list_entries()
    assert entries == list_measurement_entries(str(root))
    assert [entry["filename"] for entry in entries] == [
        "s-20261003-101500/01-room-silence.wav",
        "s-20261003-101500/02-loose.wav",
        "sessionA1/seg-1.wav",
        "sessionA1/seg-2.wav",
    ]
    assert {entry["source"]["type"] for entry in entries} == {MEASUREMENT}
    assert MeasurementSource(str(tmp_path / "none")).list_entries() == []


def test_the_configured_sources_are_voice_memos_then_measurement(tmp_path: Path) -> None:
    sources = configured_sources(tmp_path / "phone", tmp_path / "measurement", limit=3)
    assert sources == (
        VoiceMemosSource(str(tmp_path / "phone"), limit=3),
        MeasurementSource(str(tmp_path / "measurement")),
    )
    assert [source.type for source in sources] == ["voice-memos", "measurement"]
    options = index.parse_args(["--corpus-root", "p", "--measurement-root", "m", "--limit", "1"])
    assert index.index_sources(options) == (
        VoiceMemosSource(str(Path.cwd() / "p"), limit=1),
        MeasurementSource(str(Path.cwd() / "m")),
    )


def test_the_port_refuses_a_source_that_breaks_its_contract() -> None:
    def entry(recording: str, kind: str | None) -> Entry:
        return {"recordingId": recording, "filename": recording} | (
            {"source": {"type": kind}} if kind else {}
        )

    same_id_twice = Listed("alpha", [entry("x", "alpha"), entry("x", "alpha")])
    assert len(list_source_entries([same_id_twice])) == 2  # within a source, as before
    for sources, message in [
        ([Listed("alpha"), Listed("alpha")], "two sources have the type alpha"),
        ([Listed("Alpha")], "must match"),
        ([Listed("")], "must match"),
        ([Listed(None)], "must match"),
        ([Listed("alpha", [entry("x", None)])], "not tagged alpha"),
        ([Listed("alpha", [entry("x", "beta")])], "not tagged alpha"),
        ([Listed("alpha", [{"recordingId": "x", "source": "alpha"}])], "not tagged alpha"),
        (
            [Listed("alpha", [entry("x", "alpha")]), Listed("beta", [entry("x", "beta")])],
            "the alpha and beta sources both list x",
        ),
    ]:
        with pytest.raises(SourceError, match=message):
            list_source_entries(sources)


def test_a_row_without_source_is_a_voice_memos_row() -> None:
    assert source_type({"recordingId": "a"}) == "voice-memos"
    assert source_type({"source": None}) == "voice-memos"
    assert source_type({"source": {"chainId": "umik1"}}) == "voice-memos"
    assert source_type({"source": {"type": "voice-memos"}}) == "voice-memos"
    assert source_type({"source": {"type": "measurement", "sessionId": "s"}}) == "measurement"
    assert source_type(None) == "voice-memos"


# ---- the index -------------------------------------------------------------------------


def test_every_index_row_says_its_source_and_nothing_else_changes(tmp_path: Path) -> None:
    phone_drop(tmp_path)
    measurement_root(tmp_path)
    out = tmp_path / "manifests" / "recording-index.json"
    manifest = run_index(tmp_path, out)
    rows = manifest["recordings"]
    assert manifest["schemaVersion"] == "voice-journey.recording-index.v1"
    assert len(rows) == 8
    phone = [row for row in rows if row["filename"].endswith(".m4a")]
    measured = [row for row in rows if row["filename"].endswith(".wav")]
    assert len(phone) == 4 and len(measured) == 4
    assert all(row["source"] == {"type": "voice-memos"} for row in phone)
    assert {row["recordingId"]: row["source"] for row in measured} == {
        entry["recordingId"]: entry["source"]
        for entry in list_measurement_entries(str(tmp_path / "measurement"))
    }
    # Byte for byte the index the listers built, once the Voice Memos tags are taken out.
    assert jsonio.stringify(strip_phone_sources(manifest)) + "\n" == legacy_index(tmp_path, out)

    # A second run: phone states against the previous index, as before.
    (tmp_path / "phone" / "_mirror" / "latest.json").write_text(
        json.dumps({"schema": "voice-journey-mirror-manifest/1", "runId": "r2", "files": []})
    )
    previous = json.loads(out.read_text(encoding="utf-8"))
    again = run_index(tmp_path, out)
    assert {row["filename"]: row.get("phoneState") for row in again["recordings"]} == {
        MEMO_A: "deleted",
        MEMO_B: "deleted",
        MEMO_C: "unverified",
        MEMO_HELD: "unverified",  # no longer listed with another size: not held back
        MEMO_ODD: "deleted",
        "sessionA1/seg-1.wav": None,
        "sessionA1/seg-2.wav": None,
        "s-20261003-101500/01-room-silence.wav": None,
        "s-20261003-101500/02-loose.wav": None,
    }
    assert jsonio.stringify(strip_phone_sources(again)) + "\n" == legacy_index(
        tmp_path, out, previous
    )
    dry = index.build_dry_run(index.parse_args([]))
    assert "source" in dry["manifestShape"]["recordingFields"]


def test_a_third_source_plugs_in_without_touching_the_index(tmp_path: Path) -> None:
    phone_drop(tmp_path)
    measurement_root(tmp_path)
    plain = run_index(tmp_path, tmp_path / "plain.json")
    singing = FakeSingingSource(
        (("take one.flac", "2020-06-01T10:00:00Z"), ("take two.flac", "2026-10-01T10:00:00Z"))
    )

    def with_singing(options: Any) -> tuple[RecordingSource, ...]:
        return (*index.index_sources(options), singing)

    manifest = run_index(tmp_path, tmp_path / "with.json", sources=with_singing)
    rows = manifest["recordings"]
    assert manifest["totals"]["recordings"] == len(plain["recordings"]) + 2
    studio = [row for row in rows if source_type(row) == SINGING]
    assert [row["filename"] for row in studio] == ["take one.flac", "take two.flac"]
    assert studio[0]["source"] == {"type": SINGING, "studioTakeId": "take-0"}
    assert studio[0]["recordingId"] == recording_id(f"{SINGING}/take one.flac")
    assert studio[0]["file"]["extension"] == ".flac"
    assert "phoneState" not in studio[0]
    # The rows are ordered by capture time across sources; the others are unchanged.
    assert [row for row in rows if source_type(row) != SINGING] == plain["recordings"]
    assert [row["filename"] for row in rows] == [
        MEMO_ODD,
        MEMO_A,
        MEMO_B,
        "take one.flac",
        MEMO_C,
        "sessionA1/seg-1.wav",
        "sessionA1/seg-2.wav",
        "take two.flac",
        "s-20261003-101500/01-room-silence.wav",
        "s-20261003-101500/02-loose.wav",
    ]

    browser_rows = build_corpus_rows(manifest, fake_results())["rows"]
    assert {row["sourceType"] for row in browser_rows} == {VOICE_MEMOS, MEASUREMENT, SINGING}
    found = filter_rows(browser_rows, {"sourceType": SINGING})
    assert [row["filename"] for row in found] == ["take one.flac", "take two.flac"]
    # Listed, never played: the playback seam opens phone and measurement files only.
    with pytest.raises(FileNotFoundError, match="names no readable recording"):
        open_recording_audio(
            studio[0],
            corpus_root=tmp_path / "phone",
            measurement_dir=tmp_path / "measurement",
            approval="synthetic",
        )

    def colliding(options: Any) -> tuple[RecordingSource, ...]:
        clash = Listed(
            SINGING,
            [{"recordingId": recording_id(MEMO_A), "source": {"type": SINGING}}],
        )
        return (*index.index_sources(options), clash)

    with pytest.raises(SourceError, match="voice-memos and singing-studio sources both list"):
        run_index(tmp_path, tmp_path / "clash.json", sources=colliding)


def test_the_watcher_scans_the_configured_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    phone = phone_drop(tmp_path)
    measurement = measurement_root(tmp_path)
    watcher = Watchd(
        WatchOptions(
            corpus_root=str(phone),
            measurement_root=str(measurement),
            approval="synthetic",
            state_path=str(tmp_path / "state.json"),
            paths=resolve_paths({"VOICE_JOURNEY_DATA": str(tmp_path)}),
        ),
        run_stage=lambda stage, _: {},
        notify=lambda message: None,
        now=lambda: 0,
        log=lambda line: None,
    )
    entries = watcher._list_entries()
    expected = [
        *list_corpus_entries(str(phone), mirror_manifest=True),
        *list_measurement_entries(str(measurement)),
    ]
    assert [(e["recordingId"], e["sizeBytes"]) for e in entries] == [
        (e["recordingId"], e["sizeBytes"]) for e in expected
    ]
    assert [source_type(entry) for entry in entries] == [VOICE_MEMOS] * 4 + [MEASUREMENT] * 4


# ---- the stages after the index --------------------------------------------------------


@pytest.fixture
def indexes(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The index this port writes, and the same index as Node wrote it (no Voice Memos
    ``source``)."""

    phone_drop(tmp_path)
    measurement_root(tmp_path)
    tagged = run_index(tmp_path, tmp_path / "manifests" / "recording-index.json")
    return tagged, strip_phone_sources(tagged)


def _without_wall_clock(manifest: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in manifest.items() if key != "updatedAt"}


def test_the_filter_treats_voice_memos_rows_as_before(
    tmp_path: Path, indexes: tuple[dict[str, Any], dict[str, Any]]
) -> None:
    tagged, legacy = indexes
    assert vj_filter.select_sample(tagged, generated_at=GENERATED_AT) == vj_filter.select_sample(
        legacy, generated_at=GENERATED_AT
    )
    seen: dict[str, list[str]] = {}
    outputs = {}
    for name, manifest in (("tagged", tagged), ("legacy", legacy)):
        calls = seen.setdefault(name, [])

        def analyze(entry: dict[str, Any], calls: list[str] = calls) -> dict[str, Any]:
            calls.append(entry["filename"])
            return vj_filter.build_result_row(
                entry, {"activeRatio": 0.6, "voicedRatio": 0.6, "meanPitchConfidence": 0.7}
            )

        outputs[name] = vj_filter.analyze_index_manifest(
            manifest,
            approval="synthetic",
            corpus_root=str(tmp_path / "phone"),
            ffmpeg_bin="ffmpeg",
            out=str(tmp_path / f"filter-{name}.json"),
            generated_at=GENERATED_AT,
            tool_versions={"ffmpeg": None},
            analyze_recording=analyze,
        )
    assert seen["tagged"] == seen["legacy"] == [MEMO_ODD, MEMO_A, MEMO_B, MEMO_C]
    assert _without_wall_clock(outputs["tagged"]) == _without_wall_clock(outputs["legacy"])


def test_stt_treats_voice_memos_rows_as_before(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    indexes: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    tagged, legacy = indexes
    outputs = {}
    seen: dict[str, list[str]] = {}
    for name, manifest in (("tagged", tagged), ("legacy", legacy)):
        work = tmp_path / name
        (work / "local-artifacts").mkdir(parents=True)
        monkeypatch.chdir(work)
        calls = seen.setdefault(name, [])

        def transcribe(entry: dict[str, Any], options: SttOptions, calls: list[str] = calls) -> Any:
            calls.append(entry["filename"])
            return {"transcriptText": "synthetic words", "language": "en", "durationSeconds": 1}

        options = SttOptions(
            approval="synthetic",
            corpus_root=str(tmp_path / "phone"),
            out=str(work / "stt.json"),
            transcript_dir="local-artifacts/stt",
            generated_at=GENERATED_AT,
        )
        outputs[name] = transcribe_index_manifest(
            manifest, options, tool_versions={}, transcribe_recording=transcribe
        )
    assert seen["tagged"] == seen["legacy"] == [MEMO_ODD, MEMO_A, MEMO_B, MEMO_C]
    assert _without_wall_clock(outputs["tagged"]) == _without_wall_clock(outputs["legacy"])


def test_features_and_the_browser_treat_voice_memos_rows_as_before(
    indexes: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    tagged, legacy = indexes
    results = {
        "results": [
            {"recordingId": row["recordingId"], "classification": {"finalBucket": "clean_singing"}}
            for row in tagged["recordings"]
        ]
    }
    options = features.FeaturesOptions(buckets="clean_singing")
    picked = features.select_recordings(tagged, results, options)
    assert [(s.entry["recordingId"], s.bucket) for s in picked] == [
        (s.entry["recordingId"], s.bucket)
        for s in features.select_recordings(legacy, results, options)
    ]
    for selection, before in zip(
        picked, features.select_recordings(legacy, results, options), strict=True
    ):
        worker = {"status": "failed", "error": "synthetic"}
        assert features.build_feature_row(
            selection.entry, selection.bucket, worker, options
        ) == features.build_feature_row(before.entry, before.bucket, worker, options)

    rows = build_corpus_rows(tagged, results)["rows"]
    before_rows = build_corpus_rows(legacy, results)["rows"]
    assert rows == before_rows  # a row without `source` reads as voice-memos
    assert [row["sourceType"] for row in rows] == [VOICE_MEMOS] * 4 + [MEASUREMENT] * 4
    assert [row["recordingId"] for row in filter_rows(rows, {"sourceType": MEASUREMENT})] == [
        row["recordingId"] for row in tagged["recordings"] if row["source"]["type"] == MEASUREMENT
    ]
    assert project_rows(rows, "list")[0]["sourceType"] == rows[0]["sourceType"]
