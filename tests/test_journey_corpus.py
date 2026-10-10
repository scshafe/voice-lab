"""The server-side corpus listing (host-access.mjs's listing half), design §5.3.

Translated cases: the two listing cases of index-recordings.test.mjs (the mirrored drop)
and the two listing cases of host-access.test.mjs; that file's command-line cases
(describe, list dry-run, read-handle, refusals) belong to the macOS seam in apps/mirror/.
The remaining tests pin the JavaScript semantics the port reproduces.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cole_voice_lab.journey.corpus import (
    HostAccessError,
    captured_at_from_filename,
    list_corpus_entries,
    list_measurement_entries,
    mtime_iso,
    public_entry,
    read_mirror_manifest,
    recording_id,
)

MIRROR_A = "20240101 101010-AAAA1111.m4a"
MIRROR_B = "20240102 101010-BBBB2222.m4a"
MIRROR_C = "20240103 101010-CCCC3333.m4a"
SCHEMA = "voice-journey-mirror-manifest/1"


def drop(
    root: Path, files: dict[str, str], manifest_files: list[tuple[str, int]] | None = None
) -> Path:
    phone = root / "corpus" / "phone"
    (phone / "_mirror").mkdir(parents=True)
    (phone / ".rsync-partial").mkdir()
    for name, body in files.items():
        (phone / name).write_text(body)
    (phone / "CloudRecordings.db").write_text("db")
    (phone / "CloudRecordings.db-wal").write_text("wal")
    (phone / f".{MIRROR_A}.Ab12Cd").write_text("temp")
    (phone / ".rsync-partial" / MIRROR_A).write_text("partial")
    (phone / "_mirror" / "stray.m4a").write_text("not a recording")
    if manifest_files is not None:
        write_manifest(phone, manifest_files)
    return phone


def write_manifest(phone: Path, files: list[tuple[str, int]], run_id: str = "r1") -> None:
    payload = {
        "schema": SCHEMA,
        "runId": run_id,
        "files": [{"path": name, "sizeBytes": size, "sha256": "x"} for name, size in files],
    }
    (phone / "_mirror" / "latest.json").write_text(json.dumps(payload))


def snapshot(root: Path) -> list[tuple[str, int]]:
    return sorted(
        (str(path.relative_to(root)), path.stat().st_mtime_ns) for path in root.rglob("*")
    )


def session(root: Path, session_id: str, payload: object, files: dict[str, str]) -> Path:
    directory = root / session_id
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(json.dumps(payload))
    for name, body in files.items():
        (directory / name).write_text(body)
    return directory


def test_the_drop_listing_ignores_rsync_temp_files_partial_dir_and_mirror(
    tmp_path: Path,
) -> None:
    phone = drop(tmp_path, {MIRROR_A: "aaa", MIRROR_B: "bbbb"})
    before = snapshot(phone)
    entries = list_corpus_entries(phone, mirror_manifest=True)
    assert [entry["filename"] for entry in entries] == [MIRROR_A, MIRROR_B]
    assert all(entry["inManifest"] is None for entry in entries)
    assert snapshot(phone) == before


def test_a_size_differing_from_the_mirror_manifest_is_held_back_unlisted_kept(
    tmp_path: Path,
) -> None:
    phone = drop(
        tmp_path,
        {MIRROR_A: "aaa", MIRROR_B: "bbbbXX", MIRROR_C: "c"},
        [(MIRROR_A, 3), (MIRROR_B, 4)],
    )
    entries = list_corpus_entries(phone, mirror_manifest=True)
    assert [(entry["filename"], entry["inManifest"]) for entry in entries] == [
        (MIRROR_A, True),
        (MIRROR_C, False),
    ]
    assert len(list_corpus_entries(phone)) == 3


def test_list_returns_stable_metadata_for_m4a_files_only(tmp_path: Path) -> None:
    (tmp_path / "20190301 090000-0A1B2C3D.m4a").write_text("not real audio")
    (tmp_path / "notes.txt").write_text("ignore me")
    recordings = list_corpus_entries(str(tmp_path))
    assert len(recordings) == 1
    assert recordings[0]["filename"] == "20190301 090000-0A1B2C3D.m4a"
    assert recordings[0]["capturedAt"] == "2019-03-01T09:00:00Z"
    assert "hostPath" not in recordings[0]
    assert "inManifest" not in recordings[0]
    assert recordings == list_corpus_entries(tmp_path)


def test_captures_voice_memos_timestamp_from_filename() -> None:
    assert captured_at_from_filename("20190301 090000-0A1B2C3D.m4a") == "2019-03-01T09:00:00Z"
    assert captured_at_from_filename("not-a-voice-memo.m4a") is None


def test_captured_at_follows_date_utc_overflow_rules() -> None:
    assert captured_at_from_filename("20190301 129999-ZZ.m4a") == "2019-03-01T13:40:39Z"
    assert captured_at_from_filename("20190301 239999-ZZ.m4a") is None
    assert captured_at_from_filename("20190230 120000-X.m4a") is None
    assert captured_at_from_filename("20191301 120000-X.m4a") is None
    assert captured_at_from_filename("00990101 000000-Y.m4a") is None
    assert captured_at_from_filename("01000101 000000-Y.m4a") == "0100-01-01T00:00:00Z"
    assert captured_at_from_filename("20190301 090000-A\nB.m4a") is None
    assert captured_at_from_filename("20190301 090000-AB\n.m4a") is None
    assert captured_at_from_filename("20190301 090000-AB.m4a\n") == "2019-03-01T09:00:00Z"
    assert captured_at_from_filename("dir/20190301 090000-AB.M4A") == "2019-03-01T09:00:00Z"


def test_recording_id_is_the_sha256_prefix_of_the_utf8_name() -> None:
    assert recording_id("20190301 090000-0A1B2C3D.m4a") == "40990e41bcfb48b6"
    assert recording_id("a\ud800b") == "05087813392efc16"


def test_mtime_rounds_to_the_nearest_millisecond_as_node_does(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_text("x")
    base = 1_704_067_200 * 1_000_000_000
    os.utime(target, ns=(base, base + 999_700_000))
    assert mtime_iso(target.stat()) == "2024-01-01T00:00:01.000Z"
    os.utime(target, ns=(base, base + 500_000))
    assert mtime_iso(target.stat()) == "2024-01-01T00:00:00.001Z"
    os.utime(target, ns=(base, base + 499_999))
    assert mtime_iso(target.stat()) == "2024-01-01T00:00:00.001Z"
    os.utime(target, ns=(base, base + 499_800))
    assert mtime_iso(target.stat()) == "2024-01-01T00:00:00.000Z"


def test_the_corpus_root_must_be_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(HostAccessError, match="corpus root does not exist"):
        list_corpus_entries(tmp_path / "missing")
    (tmp_path / "file").write_text("x")
    with pytest.raises(HostAccessError, match="corpus root is not a directory"):
        list_corpus_entries(tmp_path / "file")


def test_listing_keeps_regular_m4a_files_only_and_applies_the_limit(tmp_path: Path) -> None:
    for name in ["b.M4A", "a.m4a", "C.m4a", ".a.m4a", "x.wav"]:
        (tmp_path / name).write_text("abc")
    (tmp_path / "dir.m4a").mkdir()
    (tmp_path / "link.m4a").symlink_to(tmp_path / "a.m4a")
    names = [entry["filename"] for entry in list_corpus_entries(tmp_path)]
    assert names == ["a.m4a", "b.M4A", "C.m4a"]
    assert list_corpus_entries(tmp_path, limit=0) == []
    assert [e["filename"] for e in list_corpus_entries(tmp_path, limit=2)] == names[:2]
    entry = list_corpus_entries(tmp_path)[0]
    assert entry["recordingId"] == recording_id("a.m4a")
    assert entry["capturedAt"] is None
    assert entry["sizeBytes"] == 3


def test_a_name_that_is_not_utf8_fails_the_listing_as_in_node(tmp_path: Path) -> None:
    with open(os.fsencode(tmp_path) + b"/x\xff.m4a", "wb") as handle:
        handle.write(b"abc")
    with pytest.raises(FileNotFoundError):
        list_corpus_entries(tmp_path)


def test_the_mirror_manifest_is_read_only_and_a_torn_or_foreign_one_is_absent(
    tmp_path: Path,
) -> None:
    phone = drop(tmp_path, {MIRROR_A: "aaa"})
    assert read_mirror_manifest(phone) is None
    latest = phone / "_mirror" / "latest.json"
    for body in ['{"schema": "', '{"schema": "other/1", "files": []}', "[]", "NaN"]:
        latest.write_text(body)
        assert read_mirror_manifest(phone) is None
    latest.write_text(json.dumps({"schema": SCHEMA, "files": {}}))
    assert read_mirror_manifest(phone) is None
    latest.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "files": [
                    {"path": MIRROR_A, "sizeBytes": 1},
                    {"path": 5},
                    None,
                    {"path": MIRROR_A, "sizeBytes": 3},
                ],
            }
        )
    )
    manifest = read_mirror_manifest(phone)
    assert manifest is not None
    assert manifest["runId"] is None
    assert manifest["files"] == {MIRROR_A: {"path": MIRROR_A, "sizeBytes": 3.0}}
    write_manifest(phone, [(MIRROR_A, 3)], run_id="r9")
    assert read_mirror_manifest(str(phone))["runId"] == "r9"  # type: ignore[index]


def test_a_manifest_size_must_be_the_same_number(tmp_path: Path) -> None:
    phone = drop(tmp_path, {MIRROR_A: "a", MIRROR_B: "b"})
    (phone / "_mirror" / "latest.json").write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "files": [
                    {"path": MIRROR_A, "sizeBytes": True},
                    {"path": MIRROR_B, "sizeBytes": 1},
                ],
            }
        )
    )
    entries = list_corpus_entries(phone, mirror_manifest=True)
    assert [(entry["filename"], entry["inManifest"]) for entry in entries] == [(MIRROR_B, True)]


def test_measurement_sessions_list_one_entry_per_segment_file(tmp_path: Path) -> None:
    root = tmp_path / "measurement"
    session(
        root,
        "sessionA1",
        {
            "chainId": "umik1",
            "startedAt": "2024-01-01T10:10:10.000Z",
            "files": [{"name": "seg-1.wav"}, {"name": "seg-2.wav"}, {"name": "gone.wav"}],
            "segments": [
                {"file": "seg-1.wav", "key": "vowel-a"},
                {"file": "seg-2.wav"},
                "noise",
                {"file": 3},
            ],
        },
        {"seg-1.wav": "w", "seg-2.wav": "ww"},
    )
    session(root, "sessionB2", {"files": [{"name": "x.WAV"}]}, {"x.WAV": "x"})
    session(root, "bad", {"files": [{"name": "x.wav"}]}, {"x.wav": "x"})
    session(root, "_sessE5", {"files": [{"name": "x.wav"}]}, {"x.wav": "x"})
    torn = session(root, "sessionC3", {}, {})
    (torn / "session.json").write_text("not json")
    (root / "sessionD4").mkdir()
    session(root, "sessionF6", "a string", {})
    session(root, "sessionG7", {"files": "", "segments": []}, {})
    (root / "loose-file.wav").write_text("x")
    entries = list_measurement_entries(root)
    assert [entry["filename"] for entry in entries] == [
        "sessionA1/seg-1.wav",
        "sessionA1/seg-2.wav",
        "sessionB2/x.WAV",
    ]
    first = entries[0]
    assert first["recordingId"] == recording_id("measurement/sessionA1/seg-1.wav")
    assert first["hostPath"] == str(root / "sessionA1" / "seg-1.wav")
    assert first["capturedAt"] == "2024-01-01T10:10:10.000Z"
    assert first["sizeBytes"] == 1
    assert first["source"] == {
        "type": "measurement",
        "chainId": "umik1",
        "sessionId": "sessionA1",
        "segmentKey": "vowel-a",
    }
    assert entries[1]["source"]["segmentKey"] is None
    assert entries[2]["capturedAt"] is None
    assert entries[2]["source"] == {
        "type": "measurement",
        "sessionId": "sessionB2",
        "segmentKey": None,
    }
    assert list(public_entry(first)) == [
        "recordingId",
        "filename",
        "capturedAt",
        "sizeBytes",
        "modifiedAt",
        "source",
    ]


def test_a_missing_measurement_root_is_empty_and_a_file_is_an_error(tmp_path: Path) -> None:
    assert list_measurement_entries(tmp_path / "none") == []
    (tmp_path / "file").write_text("x")
    with pytest.raises(NotADirectoryError):
        list_measurement_entries(tmp_path / "file")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (None, "reading 'segments'"),
        ({"segments": 5}, "map is not a function"),
        ({"segments": [None]}, "reading 'file'"),
        ({"files": 5}, "not iterable"),
        ({"files": "ab"}, "must be of type string"),
        ({"files": [None]}, "reading 'name'"),
        ({"files": [{"name": 3}]}, "must be of type string"),
    ],
)
def test_a_malformed_session_shape_fails_the_listing_as_in_node(
    tmp_path: Path, payload: object, message: str
) -> None:
    session(tmp_path, "sessionZ9", payload, {})
    with pytest.raises(TypeError, match=message):
        list_measurement_entries(tmp_path)
