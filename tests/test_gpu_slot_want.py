"""A long job's want of the GPU slot goes stale when its process, its boot or its wait is gone
(speech/gpu.py, D-046, D-049): a killed benchmark never keeps speech off the GPU."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cole_voice_lab.canonical import canonical_json_bytes
from cole_voice_lab.speech import gpu
from cole_voice_lab.speech.contracts import GpuSlotHolder, GpuSlotWant
from cole_voice_lab.speech.gpu import (
    WANTED_NAME,
    boot_id,
    process_alive,
    slot_wanted,
    want_slot,
    withdraw_want,
)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
HOLDER = GpuSlotHolder(kind="benchmark", job_id="mc-1", started_at=NOW)


def wanted(root: Path, **options: object) -> bool:
    options.setdefault("now", NOW)
    options.setdefault("alive", lambda pid: True)
    options.setdefault("boot", lambda: "boot-1")
    return slot_wanted(root, **options)  # type: ignore[arg-type]


def test_a_want_names_its_process_boot_and_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gpu, "boot_id", lambda: "boot-1")
    want_slot(tmp_path, HOLDER, wait_seconds=600, now=NOW)
    want = GpuSlotWant.model_validate_json((tmp_path / WANTED_NAME).read_bytes())
    assert (want.holder, want.pid, want.boot_id) == (HOLDER, os.getpid(), "boot-1")
    assert want.expires_at == NOW + timedelta(seconds=660)
    assert wanted(tmp_path) and wanted(tmp_path, now=NOW + timedelta(seconds=659))
    withdraw_want(tmp_path)
    assert not wanted(tmp_path)
    want_slot(tmp_path, HOLDER)  # the default wait, from now
    assert slot_wanted(tmp_path, boot=lambda: "boot-1")


@pytest.mark.parametrize(
    ("options", "reason"),
    [
        ({"alive": lambda pid: False}, "process"),
        ({"boot": lambda: "boot-2"}, "rebooted"),
        ({"now": NOW + timedelta(seconds=661)}, "past its wait"),
    ],
)
def test_a_stale_want_is_removed_and_said(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    options: dict[str, object],
    reason: str,
) -> None:
    monkeypatch.setattr(gpu, "boot_id", lambda: "boot-1")
    want_slot(tmp_path, HOLDER, wait_seconds=600, now=NOW)
    assert not wanted(tmp_path, **options)
    assert not (tmp_path / WANTED_NAME).exists()
    assert reason in capsys.readouterr().err


def test_an_unreadable_want_counts_until_it_is_old(tmp_path: Path) -> None:
    marker = tmp_path / WANTED_NAME
    marker.write_bytes(canonical_json_bytes(HOLDER))  # an older writer's marker
    now = datetime.fromtimestamp(marker.stat().st_mtime, tz=UTC)
    assert wanted(tmp_path, now=now + timedelta(minutes=5))
    assert not wanted(tmp_path, now=now + timedelta(hours=2)) and not marker.exists()


def test_the_boot_and_the_process_are_read_without_privileges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "boot_id").write_text("1234-abcd\n", encoding="ascii")
    assert boot_id(tmp_path / "boot_id") == "1234-abcd"
    assert boot_id(tmp_path / "missing") == "unknown"
    (tmp_path / "empty").write_text("", encoding="ascii")
    assert boot_id(tmp_path / "empty") == "unknown"
    assert process_alive(os.getpid())

    def gone(pid: int, signal: int) -> None:
        raise ProcessLookupError

    def foreign(pid: int, signal: int) -> None:
        raise PermissionError

    monkeypatch.setattr(os, "kill", gone)
    assert not process_alive(12345)
    monkeypatch.setattr(os, "kill", foreign)
    assert process_alive(12345)  # another user's process is alive


def test_a_want_written_while_a_stale_one_is_judged_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gpu, "boot_id", lambda: "boot-1")
    want_slot(tmp_path, HOLDER, wait_seconds=600, now=NOW)
    fresh = GpuSlotHolder(kind="benchmark", job_id="mc-2", started_at=NOW)

    def replaced_meanwhile(pid: int) -> bool:
        # The old want's process is gone, and a new job writes its want right then.
        want_slot(tmp_path, fresh, wait_seconds=600, now=NOW)
        return False

    assert wanted(tmp_path, alive=replaced_meanwhile)  # the new want counts
    kept = GpuSlotWant.model_validate_json((tmp_path / WANTED_NAME).read_bytes())
    assert kept.holder == fresh
    assert sorted(path.name for path in tmp_path.iterdir()) == [WANTED_NAME]


def test_a_marker_that_is_not_a_file_is_judged_by_its_age(tmp_path: Path) -> None:
    (tmp_path / "elsewhere").write_text("x", encoding="ascii")
    (tmp_path / WANTED_NAME).symlink_to(tmp_path / "elsewhere")
    now = datetime.fromtimestamp((tmp_path / WANTED_NAME).lstat().st_mtime, tz=UTC)
    assert wanted(tmp_path, now=now + timedelta(minutes=5))
    assert not wanted(tmp_path, now=now + timedelta(hours=2))
    assert not os.path.lexists(tmp_path / WANTED_NAME) and (tmp_path / "elsewhere").exists()
