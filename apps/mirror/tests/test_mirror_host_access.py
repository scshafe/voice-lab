"""host-access.test.mjs, translated (design §5.3), then the seam's remaining branches."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from voice_lab_mirror import host_access
from voice_lab_mirror.host_access import (
    HostAccessError,
    RefusedOperationError,
    captured_at_from_filename,
    collation_key,
    iso_mtime,
    js_number,
    js_resolve,
    list_corpus_entries,
    list_metadata_database_files,
    main,
    node_extname,
    open_corpus_file_for_read,
    recording_id,
    run,
)

NAME = "20190301 090000-0A1B2C3D.m4a"


def capture_run(capsys: pytest.CaptureFixture[str], args: list[str]) -> tuple[int, str, str]:
    code = run(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_describe_is_read_only_and_inspectable(capsys: pytest.CaptureFixture[str]) -> None:
    code, stdout, _ = capture_run(capsys, ["--corpus-root", "/tmp/example", "describe"])
    assert code == 0
    payload = json.loads(stdout)
    assert payload["operations"] == ["describe", "list", "metadata", "read-handle"]
    assert payload["privacy"]["uploadsAudio"] is False
    assert payload["privacy"]["mutatesCorpus"] is False
    assert payload["privacy"]["readsAudioBytes"] is False
    assert "delete" in payload["refusedOperations"]


def test_list_dry_run_does_not_require_an_existing_corpus(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, stdout, _ = capture_run(capsys, ["--corpus-root", "/does/not/exist", "list", "--dry-run"])
    assert code == 0
    payload = json.loads(stdout)
    assert payload["dryRun"] is True
    assert payload["operation"] == "list"
    assert payload["willReadAudioBytes"] is False


def test_list_returns_stable_metadata_for_m4a_files_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / NAME).write_text("not real audio")
    (tmp_path / "notes.txt").write_text("ignore me")

    code, stdout, _ = capture_run(capsys, ["--corpus-root", str(tmp_path), "list"])

    assert code == 0
    payload = json.loads(stdout)
    assert payload["count"] == 1
    assert payload["recordings"][0]["filename"] == NAME
    assert payload["recordings"][0]["capturedAt"] == "2019-03-01T09:00:00Z"
    assert "hostPath" not in payload["recordings"][0]


def test_read_handle_requires_approval_and_includes_no_audio(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / NAME).write_text("not real audio")

    with pytest.raises(HostAccessError, match="requires --approval"):
        run(["--corpus-root", str(tmp_path), "read-handle", NAME])

    code, stdout, _ = capture_run(
        capsys,
        [
            "--corpus-root",
            str(tmp_path),
            "read-handle",
            NAME,
            "--approval",
            "operator-approved sample",
        ],
    )

    assert code == 0
    payload = json.loads(stdout)
    assert payload["handle"]["readOnly"] is True
    assert payload["handle"]["audioBytesIncluded"] is False
    assert payload["handle"]["approval"] == "operator-approved sample"


def test_refuses_mutation_or_upload_commands() -> None:
    with pytest.raises(RefusedOperationError, match="refusing unsupported"):
        run(["delete", "anything"])
    with pytest.raises(RefusedOperationError, match="refusing unsupported"):
        run(["--corpus-root", "/tmp/example", "upload"])


def test_captures_voice_memos_timestamp_from_filename() -> None:
    assert captured_at_from_filename(NAME) == "2019-03-01T09:00:00Z"
    assert captured_at_from_filename("not-a-voice-memo.m4a") is None


# Beyond the JavaScript file: the rest of the seam.


def test_captured_at_follows_date_utc_carry_and_calendar_checks() -> None:
    assert captured_at_from_filename("20190301 126000-X.m4a") == "2019-03-01T13:00:00Z"
    assert captured_at_from_filename("20190301 000099-X.m4a") == "2019-03-01T00:01:39Z"
    assert captured_at_from_filename("20190301 240000-X.m4a") is None  # next day
    assert captured_at_from_filename("20190230 090000-X.m4a") is None
    assert captured_at_from_filename("20191301 090000-X.m4a") is None
    assert captured_at_from_filename("00190301 090000-X.m4a") is None  # 1919 in JavaScript
    assert captured_at_from_filename("20190301 090000-.m4a") is None
    assert captured_at_from_filename("20190301 090000-X\n.m4a") is None
    assert captured_at_from_filename("2019030١ 090000-X.m4a") is None  # \d is ASCII
    assert captured_at_from_filename("dir/20190301 090000-X") == "2019-03-01T09:00:00Z"


def test_node_extname_and_resolve_match_node() -> None:
    assert node_extname("a.M4A") == ".M4A"
    assert node_extname(".m4a") == ""
    assert node_extname("..m4a") == ".m4a"
    assert node_extname("..") == ""
    assert node_extname("...") == "."
    assert node_extname("noext") == ""
    assert js_resolve("//x//y/") == "/x/y"
    assert js_resolve("/a/../b") == "/b"


def test_collation_is_icu_order_for_ascii_names() -> None:
    names = ["b", "B", "a1", "A0", "_x", "-x", "1", "a", " z", "\tz", "$", "~"]
    assert sorted(names, key=collation_key) == [
        "\tz",
        " z",
        "_x",
        "-x",
        "~",
        "$",
        "1",
        "a",
        "A0",
        "a1",
        "b",
        "B",
    ]


def test_collation_ignores_control_characters_and_puts_other_scripts_last() -> None:
    assert collation_key("\x01a")[:2] == collation_key("a")[:2]
    assert collation_key("é") > collation_key("zzz") > collation_key("~")


def test_js_number_follows_number_of_a_string() -> None:
    assert js_number(" 5 ") == 5
    assert js_number("") == 0
    assert js_number("0x10") == 16
    assert js_number("1e2") == 100
    assert js_number("-Infinity") == float("-inf")
    for text in ("1_0", "inf", "-0x10", "abc"):
        assert js_number(text) != js_number(text)  # NaN


def test_iso_mtime_rounds_like_node(tmp_path: Path) -> None:
    target = tmp_path / "f"
    target.write_bytes(b"")
    for nanoseconds, expected in (
        (1_700_000_000_000_600_000, "2023-11-14T22:13:20.001Z"),
        (1_700_000_000_000_400_000, "2023-11-14T22:13:20.000Z"),
        (1_700_000_000_999_500_000, "2023-11-14T22:13:21.000Z"),
        (1_700_000_000_000_499_999, "2023-11-14T22:13:20.001Z"),
    ):
        os.utime(target, ns=(nanoseconds, nanoseconds))
        assert iso_mtime(target.stat()) == expected


def test_listing_skips_dotfiles_symlinks_directories_and_other_types(tmp_path: Path) -> None:
    (tmp_path / "b.M4A").write_bytes(b"12")
    (tmp_path / "A.m4a").write_bytes(b"1")
    (tmp_path / ".hidden.m4a").write_bytes(b"1")
    (tmp_path / "dir.m4a").mkdir()
    (tmp_path / "link.m4a").symlink_to(tmp_path / "A.m4a")
    entries = list_corpus_entries(str(tmp_path))
    assert [entry["filename"] for entry in entries] == ["A.m4a", "b.M4A"]
    assert entries[0]["recordingId"] == recording_id("A.m4a")
    assert entries[0]["capturedAt"] is None
    assert entries[1]["sizeBytes"] == 2
    assert list_corpus_entries(str(tmp_path), limit=1) == entries[:1]


def test_list_limit_is_a_non_negative_integer(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / NAME).write_bytes(b"1")
    code, stdout, _ = capture_run(capsys, ["--corpus-root", str(tmp_path), "list", "--limit", "0"])
    assert code == 0 and json.loads(stdout)["count"] == 0
    for bad in ("-1", "1.5", "x", "Infinity"):
        with pytest.raises(HostAccessError, match="non-negative integer"):
            run(["--corpus-root", str(tmp_path), "list", "--limit", bad])
    with pytest.raises(HostAccessError, match="--limit requires a value"):
        run(["list", "--limit"])
    with pytest.raises(HostAccessError, match="--corpus-root requires a value"):
        run(["--corpus-root", "--dry-run", "list"])


def test_metadata_finds_by_id_or_name_and_has_a_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / NAME).write_bytes(b"123")
    root = ["--corpus-root", str(tmp_path)]
    code, stdout, _ = capture_run(capsys, [*root, "metadata", recording_id(NAME)])
    assert code == 0 and json.loads(stdout)["recording"]["filename"] == NAME
    code, stdout, _ = capture_run(capsys, [*root, "metadata", "x.m4a", "--dry-run"])
    assert json.loads(stdout) == {
        "dryRun": True,
        "operation": "metadata",
        "corpusRoot": str(tmp_path),
        "allowedExtensions": [".m4a"],
        "willReadAudioBytes": False,
        "wouldReadDirectory": True,
        "wouldStatMatchedFile": True,
        "recording": "x.m4a",
    }
    with pytest.raises(HostAccessError, match="recording not found"):
        run([*root, "metadata", "missing.m4a"])
    with pytest.raises(HostAccessError, match="metadata requires"):
        run([*root, "metadata"])


def test_read_handle_dry_run_and_its_host_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / NAME).write_bytes(b"123")
    root = ["--corpus-root", str(tmp_path)]
    code, stdout, _ = capture_run(capsys, [*root, "read-handle", NAME, "--dry-run"])
    assert code == 0 and json.loads(stdout)["approval"] is None
    code, stdout, _ = capture_run(capsys, [*root, "read-handle", NAME, "--approval", "ok"])
    assert json.loads(stdout)["handle"]["hostPath"] == str(tmp_path / NAME)
    with pytest.raises(HostAccessError, match="read-handle requires a recording"):
        run([*root, "read-handle"])


def test_help_default_root_and_unsupported_operations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for args in ([], ["help"], ["--help"]):
        code, stdout, _ = capture_run(capsys, args)
        assert code == 0 and stdout.startswith("Read-only host-access seam")
    monkeypatch.setenv(host_access.CORPUS_ROOT_ENV, str(tmp_path))
    _, stdout, _ = capture_run(capsys, ["describe"])
    assert json.loads(stdout)["corpusRoot"] == str(tmp_path)
    monkeypatch.delenv(host_access.CORPUS_ROOT_ENV)
    _, stdout, _ = capture_run(capsys, ["describe"])
    assert json.loads(stdout)["corpusRoot"].endswith("group.com.apple.VoiceMemos.shared/Recordings")
    with pytest.raises(HostAccessError, match="unsupported operation: frobnicate"):
        run(["frobnicate"])
    with pytest.raises(RefusedOperationError):
        run(["--dry-run", "rm"])


def test_a_missing_or_non_directory_root_is_a_host_access_error(tmp_path: Path) -> None:
    with pytest.raises(HostAccessError, match="does not exist"):
        list_corpus_entries(str(tmp_path / "missing"))
    (tmp_path / "file").write_bytes(b"")
    with pytest.raises(HostAccessError, match="not a directory"):
        list_metadata_database_files(str(tmp_path / "file"))


def test_metadata_database_files_are_listed_when_regular(tmp_path: Path) -> None:
    (tmp_path / "CloudRecordings.db").write_bytes(b"db")
    (tmp_path / "CloudRecordings.db-wal").mkdir()
    files = list_metadata_database_files(str(tmp_path))
    assert [(file.name, file.size_bytes) for file in files] == [("CloudRecordings.db", 2)]


def test_open_for_read_accepts_recordings_and_the_database(tmp_path: Path) -> None:
    (tmp_path / "a.M4A").write_bytes(b"abc")
    (tmp_path / "CloudRecordings.db-shm").write_bytes(b"x")
    with open_corpus_file_for_read(str(tmp_path), "a.M4A") as handle:
        assert handle.read() == b"abc"
    with open_corpus_file_for_read(str(tmp_path), "CloudRecordings.db-shm") as handle:
        assert handle.read() == b"x"
    for name in ("", "..", "a.m4a/", "CloudRecordings.db-journal"):
        with pytest.raises(RefusedOperationError):
            open_corpus_file_for_read(str(tmp_path), name)


def test_main_maps_refusals_to_2_and_failures_to_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["delete"]) == 2
    assert "refusing unsupported" in capsys.readouterr().err
    assert main(["--corpus-root", str(tmp_path / "missing"), "list"]) == 1
    assert capsys.readouterr().err.startswith("error: corpus root does not exist")
    assert main(["--corpus-root", "/tmp/example", "describe"]) == 0
