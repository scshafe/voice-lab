"""index-recordings.test.mjs, translated (design §5.3). The file's two listing cases
(the mirrored drop) test host-access's listing and are in tests/test_journey_corpus.py.
The remaining tests cover the command line and the JavaScript semantics of the port."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab.journey import index, jsonio
from cole_voice_lab.journey.index import (
    build_manifest,
    main,
    mark_phone_state,
    run,
)

MIRROR_A = "20240101 101010-AAAA1111.m4a"
MIRROR_B = "20240102 101010-BBBB2222.m4a"
MIRROR_C = "20240103 101010-CCCC3333.m4a"
SCHEMA = "voice-journey-mirror-manifest/1"
GENERATED_AT = "2026-07-16T00:00:00.000Z"


def capture(capsys: pytest.CaptureFixture[str], args: list[str]) -> tuple[int, str]:
    code = run(args)
    return code, capsys.readouterr().out


def drop(root: Path, files: dict[str, str], manifest_files: list[tuple[str, int]]) -> Path:
    phone = root / "corpus" / "phone"
    (phone / "_mirror").mkdir(parents=True)
    (phone / ".rsync-partial").mkdir()
    for name, body in files.items():
        (phone / name).write_text(body)
    (phone / f".{MIRROR_A}.Ab12Cd").write_text("temp")
    (phone / ".rsync-partial" / MIRROR_A).write_text("partial")
    (phone / "_mirror" / "stray.m4a").write_text("not a recording")
    write_mirror_manifest(phone, manifest_files, "r1")
    return phone


def write_mirror_manifest(phone: Path, files: list[tuple[str, int]], run_id: str) -> None:
    listed = [{"path": name, "sizeBytes": size} for name, size in files]
    (phone / "_mirror" / "latest.json").write_text(
        json.dumps({"schema": SCHEMA, "runId": run_id, "files": listed})
    )


def recording(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "recordingId": "abc123",
        "filename": "20190301 090000-0A1B2C3D.m4a",
        "capturedAt": "2019-03-01T09:00:00Z",
        "sizeBytes": 12,
        "modifiedAt": "2026-01-01T00:00:00.000Z",
    }
    entry.update(overrides)
    return entry


def test_dry_run_describes_exact_metadata_only_read_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, stdout = capture(
        capsys,
        ["--corpus-root", "/tmp/corpus", "--out", "manifests/recording-index.json", "--dry-run"],
    )
    assert code == 0
    payload = json.loads(stdout)
    assert payload["operation"] == "rebuild-recording-index"
    assert payload["architecture"]["readsVia"] == ["host-access-seam", "seam-feeds-indexer"]
    assert payload["readScope"]["directoryEntries"] is True
    assert payload["readScope"]["fileStatMetadata"] is True
    assert payload["readScope"]["audioBytes"] is False
    assert payload["readScope"]["durationExtraction"] is False
    assert "contentClassification.singingStatus" in payload["manifestShape"]["classificationFields"]


def test_manifest_includes_classification_placeholders_and_tool_choices() -> None:
    manifest = build_manifest([recording()], {"generatedAt": GENERATED_AT})
    assert manifest["schemaVersion"] == "voice-journey.recording-index.v1"
    assert manifest["source"]["readScope"]["audioBytes"] is False
    assert manifest["toolChoices"][0]["openSource"] is True
    assert manifest["toolChoices"][1]["stage"] == "content-classification-placeholder"
    assert manifest["recordings"][0]["contentClassification"]["singingStatus"] == "unknown"
    assert (
        manifest["recordings"][0]["contaminationClassification"]["noiseMusicStatus"]
        == "not_evaluated"
    )
    assert manifest["recordings"][0]["durationSeconds"] is None


def test_rebuild_writes_deterministic_index_from_fake_corpus_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus_root = tmp_path / "corpus"
    out = tmp_path / "manifests" / "recording-index.json"
    corpus_root.mkdir()
    (corpus_root / "20200101 010203-AAAA.m4a").write_text("fake-audio")
    (corpus_root / "20190301 090000-0A1B2C3D.m4a").write_text("fake-audio")
    (corpus_root / "ignore.txt").write_text("ignore")
    args = [
        "--corpus-root",
        str(corpus_root),
        "--measurement-root",
        str(tmp_path / "none"),
        "--out",
        str(out),
        "--generated-at",
        GENERATED_AT,
    ]
    first = capture(capsys, args)
    first_body = out.read_text()
    second = capture(capsys, args)
    second_body = out.read_text()
    assert first[0] == 0
    assert second[0] == 0
    assert first[1] == f"{out}\n"
    assert first_body == second_body
    manifest = json.loads(first_body)
    assert manifest["totals"]["recordings"] == 2
    assert manifest["totals"]["audioBytesIndexed"] == 0
    assert [entry["filename"] for entry in manifest["recordings"]] == [
        "20190301 090000-0A1B2C3D.m4a",
        "20200101 010203-AAAA.m4a",
    ]


def test_index_from_the_mirrored_layout_deleted_on_phone_is_marked_and_kept(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    phone = drop(tmp_path, {MIRROR_A: "aaa", MIRROR_B: "bbbb"}, [(MIRROR_A, 3), (MIRROR_B, 4)])
    out = tmp_path / "manifests" / "recording-index.json"
    args = [
        "--corpus-root",
        str(phone),
        "--measurement-root",
        str(tmp_path / "none"),
        "--out",
        str(out),
    ]

    def states() -> list[tuple[str, str]]:
        capture(capsys, args)
        rows = json.loads(out.read_text())["recordings"]
        return [(row["filename"], row["phoneState"]) for row in rows]

    assert states() == [(MIRROR_A, "present"), (MIRROR_B, "present")]
    write_mirror_manifest(phone, [(MIRROR_A, 3)], "r2")
    assert states() == [(MIRROR_A, "present"), (MIRROR_B, "deleted")]
    (phone / MIRROR_C).write_text("c")
    assert states() == [(MIRROR_A, "present"), (MIRROR_B, "deleted"), (MIRROR_C, "unverified")]


def test_the_index_merges_measurement_sessions_and_writes_node_bytes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus_root = tmp_path / "phone"
    corpus_root.mkdir()
    (corpus_root / "Voice note.m4a").write_text("abc")
    (corpus_root / "20240101 101010-AAAA.m4a").write_text("abc")
    session = tmp_path / "measurement" / "sessionA1"
    session.mkdir(parents=True)
    (session / "seg-1.wav").write_text("w")
    (session / "session.json").write_text(
        json.dumps(
            {
                "chainId": "umik1",
                "startedAt": "2024-01-01T11:00:00.000Z",
                "files": [{"name": "seg-1.wav"}],
                "segments": [{"file": "seg-1.wav", "key": "vowel-a"}],
            }
        )
    )
    out = tmp_path / "index.json"
    base = ["--corpus-root", str(corpus_root), "--measurement-root", str(tmp_path / "measurement")]
    capture(capsys, [*base, "--out", str(out), "--generated-at", "G"])
    body = out.read_text()
    manifest = json.loads(body)
    rows = manifest["recordings"]
    assert [row["filename"] for row in rows] == [
        "Voice note.m4a",
        "20240101 101010-AAAA.m4a",
        "sessionA1/seg-1.wav",
    ]
    assert rows[0]["indexingStatus"] == "indexed_metadata_filename_unparsed"
    assert rows[0]["file"]["extension"] == ".m4a"
    assert "phoneState" not in rows[0]
    assert "source" not in rows[0]
    assert rows[2]["source"]["segmentKey"] == "vowel-a"
    assert rows[2]["file"]["extension"] == ".wav"
    assert list(rows[2]) == [
        "recordingId",
        "sourceRef",
        "filename",
        "capturedAt",
        "durationSeconds",
        "durationSource",
        "file",
        "source",
        "indexingStatus",
        "contentClassification",
        "contaminationClassification",
    ]
    assert body.startswith('{\n  "schemaVersion": "voice-journey.recording-index.v1",\n')
    assert '"durationSeconds": null,' in body

    code, stdout = capture(capsys, [*base, "--out", "-", "--generated-at", "G"])
    assert code == 0
    assert stdout == body
    code, stdout = capture(capsys, [*base, "--out", "-", "--generated-at", "G", "--compact"])
    assert stdout == jsonio.stringify_compact(manifest) + "\n"
    assert json.loads(stdout) == manifest
    compact = tmp_path / "deep" / "compact.json"
    capture(capsys, [*base, "--out", str(compact), "--generated-at", "G", "--compact"])
    assert compact.read_text() == stdout
    code, stdout = capture(capsys, [*base, "--out", str(out), "--limit", "1"])
    assert [row["filename"] for row in json.loads(out.read_text())["recordings"]] == [
        "20240101 101010-AAAA.m4a",
        "sessionA1/seg-1.wav",
    ]
    assert json.loads(out.read_text())["generatedAt"].endswith("Z")


def test_defaults_come_from_the_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VOICE_JOURNEY_DATA", str(tmp_path))
    for name in ("VOICE_JOURNEY_CORPUS_ROOT", "VOICE_JOURNEY_MEASUREMENT_DIR"):
        monkeypatch.delenv(name, raising=False)
    code, stdout = capture(capsys, ["--dry-run"])
    payload = json.loads(stdout)
    root = tmp_path.resolve()
    assert payload["corpusRoot"] == str(root / "corpus" / "phone")
    assert payload["measurementRoot"] == str(root / "corpus" / "measurement")
    assert payload["outputPath"] == str(root / "manifests" / "recording-index.json")
    monkeypatch.chdir(tmp_path)
    code, stdout = capture(capsys, ["--dry-run", "--corpus-root", ".//phone/", "--out", "./x"])
    payload = json.loads(stdout)
    assert payload["corpusRoot"] == f"{Path.cwd()}/phone"
    assert payload["outputPath"] == "./x"


def test_help_and_refused_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help", "--compact"]) == 0
    assert capsys.readouterr().out == index.HELP
    assert index.HELP.splitlines()[3].endswith("[--generated-at ISO] [--limit N]")
    for args, message in [
        (["--bogus"], "unsupported argument: --bogus"),
        (["--out"], "--out requires a value"),
        (["--out", "--dry-run"], "--out requires a value"),
        (["--corpus-root", ""], "--corpus-root requires a value"),
        (["--limit", "1.5"], "--limit must be a non-negative integer"),
        (["--limit", "-1"], "--limit must be a non-negative integer"),
        (["--limit", "Infinity"], "--limit must be a non-negative integer"),
        (["--limit", "ten"], "--limit must be a non-negative integer"),
        (["--help", "--generated-at"], "--generated-at requires a value"),
    ]:
        assert main(args) == 1
        captured = capsys.readouterr()
        assert captured.err == f"error: {message}\n"
        assert captured.out == ""


def test_a_missing_corpus_root_fails_with_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing"
    assert main(["--corpus-root", str(missing), "--out", str(tmp_path / "i.json")]) == 1
    assert capsys.readouterr().err == f"error: corpus root does not exist: {missing}\n"
    assert not (tmp_path / "i.json").exists()


def test_limit_is_parsed_as_javascript_number() -> None:
    options = index.parse_args(["--limit", " 0x3 ", "--measurement-root", "m", "--out", "o"])
    assert options["limit"] == 3
    assert options["measurementRoot"] == "m"
    assert index.parse_args(["--limit", "-0"])["limit"] == 0


def test_phone_state_needs_the_previous_index() -> None:
    entries: list[dict[str, Any]] = [
        {"recordingId": "a", "inManifest": None},
        {"recordingId": "b", "inManifest": True},
        {"recordingId": "c", "inManifest": False},
        {"recordingId": "d", "inManifest": False},
        {"recordingId": "e", "inManifest": False},
        {"recordingId": "f"},
    ]
    previous = {
        "recordings": [
            {"recordingId": "c", "phoneState": "present"},
            {"recordingId": "d", "phoneState": "deleted"},
            {"recordingId": "e", "phoneState": "present"},
            {"recordingId": "e", "phoneState": "unverified"},
            5,
            {"recordingId": 7},
        ]
    }
    assert mark_phone_state(entries, previous) == [
        {"recordingId": "a"},
        {"recordingId": "b", "phoneState": "present"},
        {"recordingId": "c", "phoneState": "deleted"},
        {"recordingId": "d", "phoneState": "deleted"},
        {"recordingId": "e", "phoneState": "unverified"},
        {"recordingId": "f"},
    ]
    absent: object
    for absent in (None, 5, [], {}, {"recordings": None}):
        assert mark_phone_state(entries[2:3], absent) == [
            {"recordingId": "c", "phoneState": "unverified"}
        ]
    with pytest.raises(TypeError, match="is not a function"):
        mark_phone_state(entries, {"recordings": "rows"})
    with pytest.raises(TypeError, match="reading 'recordingId'"):
        mark_phone_state(entries, {"recordings": [None]})


def test_an_unreadable_previous_index_counts_as_none(tmp_path: Path) -> None:
    out = tmp_path / "index.json"
    assert index.read_previous_index("-") is None
    assert index.read_previous_index(str(out)) is None
    out.write_text('{"recordings": [')
    assert index.read_previous_index(str(out)) is None
    out.write_text('{"recordings": []}')
    assert index.read_previous_index(str(out)) == {"recordings": []}


def test_rows_sort_by_capture_time_then_name_and_omit_undefined_fields() -> None:
    rows = build_manifest(
        [
            recording(filename="b.m4a", capturedAt=None),
            recording(filename="B.m4a", capturedAt=None),
            recording(filename="a.m4a", capturedAt="2019-03-01T09:00:00.000Z"),
            recording(filename="c.m4a", capturedAt="2019-03-01T09:00:00Z"),
            recording(filename="x.M4A", capturedAt="", source={"type": "measurement"}),
            {"filename": "bare.m4a"},
        ],
        {},
    )["recordings"]
    assert [row["filename"] for row in rows] == [
        "b.m4a",
        "B.m4a",
        "bare.m4a",
        "x.M4A",
        "a.m4a",
        "c.m4a",
    ]
    bare, unparsed = rows[2], rows[3]
    assert unparsed["indexingStatus"] == "indexed_metadata_filename_unparsed"
    assert unparsed["file"]["extension"] == ".m4a"
    assert unparsed["source"] == {"type": "measurement"}
    assert list(bare) == [
        "sourceRef",
        "filename",
        "durationSeconds",
        "durationSource",
        "file",
        "indexingStatus",
        "contentClassification",
        "contaminationClassification",
    ]
    assert bare["sourceRef"] == {"seam": "voice-journey-corpus", "filename": "bare.m4a"}
    assert bare["file"] == {"extension": ".m4a"}
    one = build_manifest([recording(capturedAt=5)], {})["recordings"]
    assert one[0]["capturedAt"] == 5
    with pytest.raises(TypeError, match="localeCompare"):
        build_manifest([recording(capturedAt=5), recording()], {})


def test_a_failed_compact_write_leaves_no_temporary_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    corpus_root = tmp_path / "phone"
    corpus_root.mkdir()
    out = tmp_path / "out"
    (out / "occupied").mkdir(parents=True)
    args = ["--corpus-root", str(corpus_root), "--measurement-root", str(tmp_path / "none")]
    assert main([*args, "--out", str(out), "--compact"]) == 1
    assert "error: " in capsys.readouterr().err
    assert sorted(path.name for path in tmp_path.iterdir()) == ["out", "phone"]
    assert [path.name for path in out.iterdir()] == ["occupied"]


def test_the_module_runs_as_a_command(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "cole_voice_lab.journey.index", "--bogus"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
    )
    assert (result.returncode, result.stdout) == (1, "")
    assert result.stderr == "error: unsupported argument: --bogus\n"


def test_index_rows_built_from_a_measurement_entry_carry_their_source() -> None:
    entry = {
        "recordingId": "r1",
        "filename": "s-1/01-x.wav",
        "capturedAt": "2026-10-03T10:15:00.000Z",
        "sizeBytes": 5,
        "modifiedAt": "2026-10-03T10:30:00.000Z",
        "source": {
            "type": "measurement",
            "chainId": "umik1",
            "sessionId": "s-1",
            "segmentKey": "x",
        },
    }
    manifest = index.build_manifest([entry], {"generatedAt": "2026-10-03T00:00:00.000Z"})
    assert manifest["recordings"][0]["source"]["type"] == "measurement"
