"""mirror.test.mjs, translated (design §5.3), then the sender's remaining branches."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from voice_lab_mirror import mirror
from voice_lab_mirror.host_access import RefusedOperationError, open_corpus_file_for_read
from voice_lab_mirror.mirror import (
    EXIT,
    RsyncResult,
    UsageError,
    is_drop_locked,
    parse_args,
    recordings_rsync_args,
    run_mirror,
    staging_rsync_args,
    validate_options,
)

NAME_A = "20240101 101010-AAAA1111.m4a"
NAME_B = "20240102 101010-BBBB2222.m4a"
ENVIRONMENT = (
    mirror.DEST_ENV,
    mirror.STATE_ENV,
    mirror.SSH_KEY_ENV,
    mirror.RSYNC_ENV,
    mirror.CORPUS_ROOT_ENV,
)


@pytest.fixture(autouse=True)
def _no_mirror_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)


@dataclass
class Fixture:
    root: Path
    corpus: Path
    rsync: Path
    logs: list[str] = field(default_factory=list)

    def argv(self, extra: list[str] | None = None) -> list[str]:
        return [
            "--corpus-root",
            str(self.corpus),
            "--state-dir",
            str(self.root / "state"),
            "--dest",
            "me@host:./",
            "--rsync-bin",
            str(self.rsync),
            "--approval",
            "test approval",
            *(extra or []),
        ]

    def run(self, extra: list[str] | None = None) -> int:
        return run_mirror(self.argv(extra), log=self.logs.append)

    def calls(self) -> list[str]:
        calls = self.root / "calls"
        return sorted(os.listdir(calls)) if calls.is_dir() else []


def make_fixture(
    tmp_path: Path, *, rsync_exit: int = 0, locked: bool = False, fail_pass: int | None = None
) -> Fixture:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / NAME_A).write_text("aaa")
    (corpus / NAME_B).write_text("bbbb")
    (corpus / "CloudRecordings.db").write_text("db")
    (corpus / "unrelated.txt").write_text("nope")
    # Fake rsync: log argv (one arg per line) and the NUL list, optionally fail.
    exit_line = (
        f"exit {rsync_exit}"
        if fail_pass is None
        else f'[ "$n" = "{fail_pass}" ] && exit {rsync_exit}; exit 0'
    )
    lock_line = (
        'echo "rrsync error: Another instance of rrsync is already accessing this directory" >&2'
        if locked
        else ""
    )
    rsync = tmp_path / "rsync-fake"
    rsync.write_text(
        f"""#!/bin/sh
mkdir -p "{tmp_path}/calls"
n=$(ls "{tmp_path}/calls" | grep -c args)
for a in "$@"; do printf '%s\\n' "$a"; done > "{tmp_path}/calls/$n.args"
for a in "$@"; do case "$a" in --files-from=*) tr '\\0' '\\n' < "${{a#--files-from=}}" \
> "{tmp_path}/calls/$n.list";; esac; done
echo "rsync: send_files failed to open \\"{NAME_A}\\"" >&2
{lock_line}
{exit_line}
"""
    )
    rsync.chmod(0o755)
    return Fixture(root=tmp_path, corpus=corpus, rsync=rsync)


def test_rsync_argument_shape_is_write_only_safe() -> None:
    options = parse_args(["--corpus-root", "/c", "--dest", "me@host:./", "--ssh-key", "/k"])
    args = recordings_rsync_args(options, "/tmp/list")
    assert args[-2:] == ["/c/", "me@host:./"]
    assert "--files-from=/tmp/list" in args and "--from0" in args
    assert "--partial-dir=.rsync-partial" in args
    assert not any(
        a.startswith("--delete") or a == "--remove-source-files" or a == "-a" for a in args
    )
    assert "--delay-updates" not in args
    ssh = args[args.index("-e") + 1]
    assert "BatchMode=yes" in ssh and ssh.endswith(" -i /k")
    staging = staging_rsync_args(options, "/s", "/tmp/l2")
    assert staging[-2:] == ["/s/", "me@host:./"]


def test_refuses_a_local_destination_and_state_inside_the_corpus() -> None:
    with pytest.raises(RefusedOperationError, match="local destination"):
        validate_options(parse_args(["--dest", "/tmp/somewhere"]))
    with pytest.raises(RefusedOperationError, match="local destination"):
        validate_options(parse_args(["--dest", "./rel"]))
    with pytest.raises(RefusedOperationError, match="inside the corpus"):
        validate_options(
            parse_args(["--corpus-root", "/c", "--state-dir", "/c/state", "--dest", "h:./"])
        )
    validate_options(parse_args(["--dest", "me@host:./"]))
    validate_options(parse_args(["--dest", "rsync://host/mod"]))


def test_the_corpus_seam_only_opens_allowlisted_files_for_reading() -> None:
    for name in ("../etc/passwd", "sub/a.m4a", "notes.txt"):
        with pytest.raises(RefusedOperationError, match="allowlist"):
            open_corpus_file_for_read("/c", name)


def test_dry_run_reads_nothing_and_runs_nothing(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    assert f.run(["--dry-run"]) == EXIT.ok
    assert f.calls() == []
    assert not (tmp_path / "state").exists()


@pytest.mark.serial
def test_real_run_needs_an_approval(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    argv = ["--corpus-root", str(f.corpus), "--state-dir", str(tmp_path / "state")]
    code = run_mirror([*argv, "--dest", "h:./"], log=lambda _line: None)
    assert code == EXIT.usage


def test_run_writes_a_verifiable_manifest_sends_recordings_then_manifest(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    assert f.run() == EXIT.ok
    assert [name for name in f.calls() if name.endswith(".list")] == ["0.list", "1.list"]
    first = [line for line in (tmp_path / "calls/0.list").read_text().split("\n") if line]
    assert sorted(first) == [NAME_A, NAME_B]  # unrelated.txt and the db are not in pass 1
    second = [line for line in (tmp_path / "calls/1.list").read_text().split("\n") if line]
    assert second[0] == "CloudRecordings.db"
    assert second[1].startswith("_mirror/manifest-") and second[1].endswith(".json")
    latest = json.loads((tmp_path / "state/staging/_mirror/latest.json").read_text())
    assert latest["recordingCount"] == 2
    a = next(x for x in latest["files"] if x["path"] == NAME_A)
    assert a["sha256"] == "9834876dcfb05cb167a5c24953eba58c4ac89b1adf57f28f2f9d09af107ee8f0"
    assert a["sizeBytes"] == 3
    assert any(x["path"] == "CloudRecordings.db" and x.get("snapshot") for x in latest["files"])
    assert len(os.listdir(tmp_path / "state/manifests")) == 1
    text = "\n".join(f.logs)
    assert "AAAA1111" not in text and ".m4a" not in text
    assert (f.corpus / NAME_A).read_text()  # source untouched


def test_unchanged_files_are_not_rehashed_changed_ones_are(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    f.run()
    f.logs.clear()
    f.run()
    assert "0 hashed" in f.logs[0]
    (f.corpus / NAME_A).write_text("changed")
    later = time.time() + 60
    os.utime(f.corpus / NAME_A, (time.time(), later))
    f.logs.clear()
    f.run()
    assert "1 hashed" in f.logs[0]


def test_a_failed_transfer_exits_4_withholds_the_manifest_pass(tmp_path: Path) -> None:
    f = make_fixture(tmp_path, rsync_exit=23)
    assert f.run() == EXIT.transfer
    assert len([name for name in f.calls() if name.endswith(".args")]) == 1  # no pass 2
    text = "\n".join(f.logs)
    assert "rsync exited 23" in text
    assert "AAAA1111" not in text


def test_lock_a_live_holder_skips_the_run_a_stale_pid_is_taken_over(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    (tmp_path / "state").mkdir(parents=True)
    lock = tmp_path / "state/mirror.lock"
    now = mirror.iso_timestamp_ms(mirror._now_ms())
    lock.write_text(json.dumps({"pid": os.getpid(), "startedAt": now}))
    assert f.run() == EXIT.locked
    assert f.calls() == []
    lock.write_text(json.dumps({"pid": 2**22 + 12345, "startedAt": now}))
    assert f.run() == EXIT.ok
    assert not lock.exists()  # released


def test_a_missing_corpus_fails_with_exit_1_and_does_not_call_rsync(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    shutil.rmtree(f.corpus)
    assert f.run() == EXIT.failure
    assert f.calls() == []
    assert f.logs == ["error: corpus root does not exist: <corpus>"]


def test_defaults_match_the_drop_account_rsync_ssh_and_modes() -> None:
    options = parse_args(["--ssh-key", "/k"])
    assert options.dest == "vj-mirror@cole-laptop:"
    assert options.rsync_bin == "/opt/homebrew/bin/rsync"
    validate_options(options)
    args = recordings_rsync_args(options, "/l")
    assert args[-1:] == ["vj-mirror@cole-laptop:"]
    assert "--chmod=D2750,F0640" in args
    ssh = args[args.index("-e") + 1]
    assert "-o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes" in ssh
    assert "-o StrictHostKeyChecking=yes" in ssh
    assert ssh.endswith(" -i /k")
    assert parse_args(["--rsync-bin", "/x/rsync"]).rsync_bin == "/x/rsync"


def test_rrsyncs_drop_lock_exits_3_and_is_not_a_transfer_failure(tmp_path: Path) -> None:
    assert is_drop_locked(
        RsyncResult(
            code=1,
            stderr="rrsync error: Another instance of rrsync is already accessing this directory\n",
        )
    )
    assert not is_drop_locked(
        RsyncResult(code=1, stderr="rsync error: some files could not be transferred")
    )
    f = make_fixture(tmp_path, rsync_exit=1, locked=True)
    assert f.run() == EXIT.locked
    assert len([name for name in f.calls() if name.endswith(".args")]) == 1  # no pass 2
    text = "\n".join(f.logs)
    assert "locked" in text
    assert "AAAA1111" not in text


# Beyond the JavaScript file: the sender's remaining branches.


def test_the_target_key_rsync_state_and_corpus_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(mirror.DEST_ENV, "vj-mirror@the-pc:")
    monkeypatch.setenv(mirror.SSH_KEY_ENV, "/keys/vj-mirror")
    monkeypatch.setenv(mirror.RSYNC_ENV, "/usr/local/bin/rsync")
    monkeypatch.setenv(mirror.STATE_ENV, str(tmp_path / "state"))
    monkeypatch.setenv(mirror.CORPUS_ROOT_ENV, str(tmp_path / "corpus"))
    options = parse_args([])
    assert options.dest == "vj-mirror@the-pc:"
    assert options.ssh_key == "/keys/vj-mirror"
    assert options.rsync_bin == "/usr/local/bin/rsync"
    assert options.state_dir == str(tmp_path / "state")
    assert options.corpus_root == str(tmp_path / "corpus")
    assert parse_args(["--dest", "vj-mirror@cole-laptop:"]).dest == "vj-mirror@cole-laptop:"
    monkeypatch.setenv(mirror.SSH_KEY_ENV, "")
    assert "-i" not in recordings_rsync_args(parse_args([]), "/l")[-5].split()


def test_dry_run_prints_both_rsync_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = make_fixture(tmp_path)
    assert f.run(["--dry-run"]) == EXIT.ok
    plan = json.loads(capsys.readouterr().out)
    assert plan["dest"] == "me@host:./"
    assert plan["neverDeletesRemotely"] is True and plan["willReadAudioBytes"] is False
    assert plan["recordingsRsync"][0] == str(f.rsync)
    assert plan["recordingsRsync"][-3:] == [
        "--files-from=<recordings-list>",
        f"{f.corpus}/",
        "me@host:./",
    ]
    assert plan["stagingRsync"][-2:] == ["<staging-dir>/", "me@host:./"]


def test_usage_errors_and_help_exit_2_and_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    logs: list[str] = []
    assert run_mirror(["--frobnicate"], log=logs.append) == EXIT.usage
    assert run_mirror(["--dest"], log=logs.append) == EXIT.usage
    assert run_mirror(["--dest", "--dry-run"], log=logs.append) == EXIT.usage
    assert logs == [
        "error: unknown argument: --frobnicate",
        "error: --dest requires a value",
        "error: --dest requires a value",
    ]
    for argv in (["--help"], ["help"]):
        assert run_mirror(argv, log=logs.append) == EXIT.ok
        assert capsys.readouterr().out.startswith("Mirror the Voice Memos corpus")
    with pytest.raises(UsageError):
        parse_args(["--state-dir"])


def test_a_failed_manifest_pass_exits_4(tmp_path: Path) -> None:
    f = make_fixture(tmp_path, rsync_exit=12, fail_pass=1)
    assert f.run() == EXIT.transfer
    assert (
        f.logs[-1] == "manifest transfer failed: rsync exited 12; 1 diagnostic line(s) suppressed"
    )
    assert not (tmp_path / "state/hashes.json").exists()  # the next run rehashes


def test_a_locked_manifest_pass_exits_3(tmp_path: Path) -> None:
    f = make_fixture(tmp_path, rsync_exit=1, locked=True, fail_pass=1)
    assert f.run() == EXIT.locked
    assert "locked" in f.logs[-1]


def test_no_recordings_skips_pass_1_and_still_sends_the_manifest(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    (f.corpus / NAME_A).unlink()
    (f.corpus / NAME_B).unlink()
    (f.corpus / "CloudRecordings.db").unlink()
    assert f.run() == EXIT.ok
    assert [name for name in f.calls() if name.endswith(".args")] == ["0.args"]
    sent = (tmp_path / "calls/0.list").read_text().split("\n")
    assert sent[0].startswith("_mirror/manifest-") and sent[1] == "_mirror/latest.json"


def test_an_rsync_that_cannot_start_or_dies_by_signal_is_a_transfer_failure(
    tmp_path: Path,
) -> None:
    f = make_fixture(tmp_path)
    f.rsync.unlink()
    assert f.run() == EXIT.transfer
    assert "rsync exited 127; 1 diagnostic line(s) suppressed" in f.logs[-1]
    f.rsync.write_text("#!/bin/sh\nkill -TERM $$\n")
    f.rsync.chmod(0o755)
    assert f.run() == EXIT.transfer
    assert "rsync exited 128 (SIGTERM); 0 diagnostic line(s)" in f.logs[-1]


def test_lock_edge_cases_old_unreadable_and_foreign_holders(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    (tmp_path / "state").mkdir()
    lock = tmp_path / "state/mirror.lock"
    holders: list[Callable[[], None]] = [
        lambda: lock.write_text(
            json.dumps({"pid": os.getpid(), "startedAt": "2000-01-01T00:00:00.000Z"})
        ),
        lambda: lock.write_text("{torn"),
        lambda: lock.write_text("[1]"),
        lambda: lock.write_text(json.dumps({"pid": os.getpid(), "startedAt": "not a date"})),
        lambda: lock.write_text(json.dumps({"pid": True, "startedAt": "2099-01-01"})),
        lambda: lock.write_text(json.dumps({"pid": 1.5, "startedAt": "2099-01-01"})),
        lambda: lock.write_text(json.dumps({"pid": 2**40, "startedAt": "2099-01-01"})),
    ]
    for write in holders:
        write()
        assert f.run() == EXIT.ok
        assert not lock.exists()
    lock.write_text(json.dumps({"pid": float(os.getpid()), "startedAt": "2099-01-01"}))
    assert f.run() == EXIT.locked


def test_a_holder_that_is_another_users_process_counts_as_alive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(_pid: int, _signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(mirror.os, "kill", denied)
    assert mirror._process_alive(1)


def test_a_lock_that_keeps_reappearing_gives_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def always_exists(*_args: object) -> int:
        raise FileExistsError

    monkeypatch.setattr(mirror.os, "open", always_exists)
    assert mirror._acquire_lock(str(tmp_path / "mirror.lock")) is False


def test_a_corrupt_hash_cache_rehashes_everything(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    for cache in ("null", '{"files": []}', json.dumps({"files": {NAME_A: 1}})):
        (tmp_path / "state").mkdir(exist_ok=True)
        (tmp_path / "state/hashes.json").write_text(cache)
        f.logs.clear()
        assert f.run() == EXIT.ok
        assert "2 hashed" in f.logs[0]


def test_a_staging_path_that_is_a_file_is_replaced(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    (tmp_path / "state").mkdir()
    (tmp_path / "state/staging").write_text("stray")
    assert f.run() == EXIT.ok
    assert (tmp_path / "state/staging/_mirror/latest.json").is_file()
    cache = json.loads((tmp_path / "state/hashes.json").read_text())
    assert sorted(cache["files"]) == [NAME_A, NAME_B]
    assert (tmp_path / "state/hashes.json").read_text().startswith('{"files":{"')


def test_an_os_error_during_the_run_exits_1_without_naming_files(tmp_path: Path) -> None:
    f = make_fixture(tmp_path)
    (f.corpus / NAME_A).chmod(0)
    try:
        if os.access(f.corpus / NAME_A, os.R_OK):
            pytest.skip("running with permission to read a mode-0 file")
        assert f.run() == EXIT.failure
    finally:
        (f.corpus / NAME_A).chmod(stat.S_IRUSR | stat.S_IWUSR)
    assert f.logs[-1] == "error: EACCES during the run; details withheld, the next run retries"
    assert "AAAA1111" not in "\n".join(f.logs)
    assert not (tmp_path / "state/mirror.lock").exists()
    assert f.calls() == []


def test_main_runs_the_mirror_from_argv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    f = make_fixture(tmp_path)
    assert mirror.main([*f.argv(), "--dry-run"]) == EXIT.ok
    assert json.loads(capsys.readouterr().out)["operation"] == "mirror"
    assert mirror.main(["--bogus"]) == EXIT.usage
    assert capsys.readouterr().err == "[mirror] error: unknown argument: --bogus\n"


def test_state_equal_to_the_corpus_is_refused() -> None:
    with pytest.raises(RefusedOperationError, match="inside the corpus"):
        validate_options(parse_args(["--corpus-root", "/c", "--state-dir", "/c/", "--dest", "h:"]))


def test_a_lock_removed_during_the_run_is_not_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = make_fixture(tmp_path)
    real = mirror._mirror_locked

    def remove_lock_then_run(options: mirror.Options, log: Callable[[str], None]) -> int:
        (tmp_path / "state/mirror.lock").unlink()
        return real(options, log)

    monkeypatch.setattr(mirror, "_mirror_locked", remove_lock_then_run)
    assert f.run() == EXIT.ok


def test_python_dash_m_voice_lab_mirror_runs_the_mirror(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["voice_lab_mirror", "--help"])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("voice_lab_mirror", run_name="__main__", alter_sys=True)
    assert stopped.value.code == EXIT.ok
    assert capsys.readouterr().out.startswith("Mirror the Voice Memos corpus")
