"""watchd.test.mjs, translated (design §5.3), plus the job runner's one-shot mode, the
notification outbox, the stage runner and the command lines."""

from __future__ import annotations

import importlib
import json
import os
import signal
import stat
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from cole_voice_lab import cli
from cole_voice_lab.jobs import watch
from cole_voice_lab.jobs.watch import (
    Notification,
    Stage,
    StageContext,
    StageFailure,
    Watchd,
    WatchError,
    WatchOptions,
    build_stages,
    count_new_since_index,
    default_run_stage,
    digest_diff_lines,
    digest_snapshot_from_manifests,
    next_digest_at,
    scan_signature,
    send_ntfy,
    write_outbox,
)
from cole_voice_lab.journey.paths import resolve_paths

PIPELINE_ORDER = ["index", "filter", "stt", "lyric", "reclassify", "features", "journeys"]
DAY_MS = 86_400_000
PHONE_A = "20240101 101010-AAAA1111.m4a"


def entry(recording_id: str, size_bytes: int) -> dict[str, Any]:
    return {"recordingId": recording_id, "filename": f"{recording_id}.m4a", "sizeBytes": size_bytes}


@dataclass
class Context:
    calls: list[str] = field(default_factory=list)
    notices: list[Notification] = field(default_factory=list)
    reloads: int = 0
    clock: int = 1_000_000_000_000
    entries: list[dict[str, Any]] = field(default_factory=lambda: [entry("a", 10)])
    logs: list[str] = field(default_factory=list)


def make_watchd(
    root: Path,
    context: Context | None = None,
    *,
    run_stage: watch.RunStage | None = None,
    notify: watch.Notify | None | bool = True,
    list_entries: Callable[[], list[dict[str, Any]]] | None = None,
    **overrides: Any,
) -> tuple[Watchd, Context]:
    ctx = context or Context()

    def default_stage(stage: Stage, _: StageContext) -> dict[str, str]:
        ctx.calls.append(stage.key)
        return {}

    def reload() -> None:
        ctx.reloads += 1

    options: dict[str, Any] = {
        "corpus_root": "/fake-corpus",
        "approval": "test approval",
        "state_path": str(root / "watchd-state.json"),
        "work_dir": str(root),
        "ntfy_url": "http://ntfy.test/topic",
        "paths": resolve_paths({}),
        **overrides,
    }
    watcher = Watchd(
        WatchOptions(**options),
        list_entries=list_entries or (lambda: ctx.entries),
        run_stage=run_stage or default_stage,
        notify=ctx.notices.append if notify is True else (notify or None),
        now=lambda: ctx.clock,
        on_ingested=reload,
        log=ctx.logs.append,
    )
    return watcher, ctx


def read_state(root: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((root / "watchd-state.json").read_text())
    return payload


def dead_pid() -> int:
    child = subprocess.Popen(["true"])
    child.wait()
    return child.pid


# ---- watchd.test.mjs ----------------------------------------------------------------------


def test_scan_signature_is_order_insensitive_and_size_sensitive() -> None:
    entries = [entry("a", 1), entry("b", 2)]
    assert scan_signature(entries) == scan_signature(list(reversed(entries)))
    assert scan_signature(entries) != scan_signature([entry("a", 1), entry("b", 3)])
    assert scan_signature(entries) != scan_signature([entry("a", 1)])


def test_count_new_since_index_counts_unindexed_and_resized_recordings() -> None:
    index = {"recordings": [{"recordingId": "a", "file": {"sizeBytes": 10}}]}
    assert count_new_since_index([entry("a", 10)], index) == 0
    assert count_new_since_index([entry("a", 10), entry("b", 5)], index) == 1
    assert count_new_since_index([entry("a", 99)], index) == 1
    assert count_new_since_index([entry("a", 10), entry("b", 5)], None) == 2


def test_build_stages_keeps_order_wires_approval_only_into_audio_stages_and_real_modules() -> None:
    stages = build_stages("/corpus", "approved")
    assert [stage.key for stage in stages] == PIPELINE_ORDER
    assert [stage.key for stage in stages if "--approval" in stage.args] == [
        "filter",
        "stt",
        "features",
    ]
    assert [stage.key for stage in stages if "--corpus-root" in stage.args] == [
        "index",
        "filter",
        "stt",
        "features",
    ]
    for stage in stages:
        module = importlib.import_module(f"cole_voice_lab.journey.{stage.module}")
        assert callable(module.main)


def test_watchd_waits_for_two_agreeing_scans_runs_in_order_and_does_not_rerun(
    tmp_path: Path,
) -> None:
    watcher, context = make_watchd(tmp_path)
    first = watcher.tick()
    assert first["status"] == "unstable"
    assert context.calls == []

    second = watcher.tick()
    assert second["status"] == "ran"
    assert context.calls == PIPELINE_ORDER
    assert context.reloads == 1
    assert any("ingested" in notice["title"] for notice in context.notices)
    assert (tmp_path / "watchd-state.json").is_file()

    third = watcher.tick()
    assert third["status"] == "idle"
    assert context.calls == PIPELINE_ORDER
    assert context.reloads == 1


def test_a_failing_stage_aborts_notifies_backs_off_and_retries_on_corpus_change(
    tmp_path: Path,
) -> None:
    fail_stt = True
    context = Context()

    def run_stage(stage: Stage, _: StageContext) -> dict[str, str]:
        context.calls.append(stage.key)
        if stage.key == "stt" and fail_stt:
            raise StageFailure("stt: exit 1", "boom")
        return {}

    watcher, _ = make_watchd(tmp_path, context, run_stage=run_stage)
    watcher.tick()
    failed = watcher.tick()
    assert failed["status"] == "failed"
    assert context.calls == ["index", "filter", "stt"]
    assert any("stage failed" in notice["title"] for notice in context.notices)
    assert context.reloads == 0

    backoff = watcher.tick()
    assert backoff["status"] == "backoff"
    assert context.calls == ["index", "filter", "stt"]

    fail_stt = False
    context.calls.clear()
    context.entries = [*context.entries, entry("b", 20)]
    assert watcher.tick()["status"] == "unstable"
    assert watcher.tick()["status"] == "ran"
    assert context.calls == PIPELINE_ORDER


def test_a_live_lock_excludes_the_pipeline_and_a_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    live = tmp_path / "live"
    live.mkdir()
    (live / "watchd-state.json.lock").write_text(json.dumps({"pid": 1, "startedAt": "now"}) + "\n")
    watcher, context = make_watchd(live)
    watcher.tick()
    assert watcher.tick()["status"] == "locked"
    assert context.calls == []

    stale = tmp_path / "stale"
    stale.mkdir()
    lock = stale / "watchd-state.json.lock"
    lock.write_text(json.dumps({"pid": dead_pid(), "startedAt": "now"}) + "\n")
    watcher, context = make_watchd(stale)
    watcher.tick()
    assert watcher.tick()["status"] == "ran"
    assert context.calls == PIPELINE_ORDER
    assert not lock.exists()


def local_ms(*parts: int) -> int:
    return round(datetime(*parts).timestamp() * 1000)  # type: ignore[misc]


def test_next_digest_at_lands_on_the_configured_local_weekday_and_hour() -> None:
    wednesday = local_ms(2026, 8, 12, 12, 0, 0)
    following = datetime.fromtimestamp(next_digest_at(wednesday, "sunday", 18) / 1000)
    assert following.weekday() == 6
    assert following.hour == 18
    next_ms = next_digest_at(wednesday, "sunday", 18)
    assert 0 < next_ms - wednesday < 8 * DAY_MS

    sunday_before = local_ms(2026, 8, 16, 17, 0, 0)
    assert next_digest_at(sunday_before, "sunday", 18) - sunday_before == 3_600_000

    sunday_after = local_ms(2026, 8, 16, 19, 0, 0)
    after_ms = next_digest_at(sunday_after, "sunday", 18)
    assert datetime.fromtimestamp(after_ms / 1000).weekday() == 6
    assert after_ms - sunday_after > 6 * DAY_MS


def test_digest_snapshot_and_diff_carry_counts_only() -> None:
    snapshot = digest_snapshot_from_manifests(
        results={
            "results": [
                {"classification": {"finalBucket": "clean_singing"}},
                {"classification": {"finalBucket": "noise"}},
                {"classification": {"finalBucket": "clean_singing"}},
            ]
        },
        transcripts={"transcripts": [{}, {}]},
        lyrics={"clusters": [{}]},
        journeys={
            "journeys": [{}, {}, {}],
            "improvementIndex": [{"verdict": "flat"}, {"verdict": "flat"}],
        },
    )
    assert snapshot == {
        "rows": 3,
        "buckets": {"clean_singing": 2, "noise": 1},
        "transcribed": 2,
        "clusters": 1,
        "songJourneys": 3,
        "verdicts": {"flat": 2},
    }
    assert sorted(snapshot) == [
        "buckets",
        "clusters",
        "rows",
        "songJourneys",
        "transcribed",
        "verdicts",
    ]
    fresh = digest_diff_lines(None, snapshot)
    assert any("takes classified: 3" in line for line in fresh)
    delta = digest_diff_lines({**snapshot, "rows": 1}, snapshot)
    assert any("takes classified: 3 (+2)" in line for line in delta)


def test_the_weekly_digest_fires_once_the_scheduled_time_passes_and_reschedules(
    tmp_path: Path,
) -> None:
    (tmp_path / "manifests").mkdir()
    (tmp_path / "manifests" / "recording-index.json").write_text(
        json.dumps({"recordings": [{"recordingId": "a", "file": {"sizeBytes": 10}}]})
    )
    watcher, context = make_watchd(tmp_path)
    watcher.tick()
    assert watcher.tick()["status"] == "idle"
    assert context.notices == []

    context.clock += 9 * DAY_MS
    watcher.tick()
    digests = [notice for notice in context.notices if "weekly digest" in notice["title"]]
    assert len(digests) == 1
    assert "takes classified: 0" in digests[0]["body"]

    watcher.tick()
    assert len([n for n in context.notices if "weekly digest" in n["title"]]) == 1


# ---- the one-shot mode, persisted across processes ----------------------------------------


def test_one_shot_runs_agree_across_processes_through_the_state_file(tmp_path: Path) -> None:
    context = Context()
    first = make_watchd(tmp_path, context)[0].run_once()
    assert first["status"] == "unstable"
    state = read_state(tmp_path)
    assert state["pendingSignature"] == "a:10"
    assert state["lastTick"]["status"] == "unstable"
    assert list(state)[:6] == [
        "version",
        "lastRun",
        "recentRuns",
        "lastDigest",
        "nextDigestAt",
        "failure",
    ]

    assert make_watchd(tmp_path, context)[0].run_once()["status"] == "ran"
    assert context.calls == PIPELINE_ORDER
    assert make_watchd(tmp_path, context)[0].run_once() == {
        "at": "2001-09-09T01:46:40.000Z",
        "status": "idle",
        "newCount": 1,
    }

    context.entries = [entry("a", 10), entry("b", 1)]
    assert make_watchd(tmp_path, context)[0].run_once()["status"] == "unstable"
    context.entries = [entry("a", 10), entry("b", 2)]
    assert make_watchd(tmp_path, context)[0].run_once()["status"] == "unstable"
    assert make_watchd(tmp_path, context)[0].run_once()["status"] == "ran"
    state = read_state(tmp_path)
    assert state["lastRun"]["signature"] == "a:10|b:2"
    assert [run["newCount"] for run in state["recentRuns"]] == [1, 2]


def test_one_shot_counts_scan_failures_across_runs_and_notifies_on_the_third(
    tmp_path: Path,
) -> None:
    context = Context()

    def failing() -> list[dict[str, Any]]:
        raise OSError("corpus unreadable")

    for _ in range(4):
        result = make_watchd(tmp_path, context, list_entries=failing)[0].run_once()
        assert result["status"] == "scan-error"
    assert [notice["title"] for notice in context.notices] == [
        "Voice Journey watchd: corpus scan failing"
    ]
    assert "corpus unreadable" in context.notices[0]["body"]
    assert read_state(tmp_path)["scanErrorStreak"] == 4
    assert "scan failed: corpus unreadable" in context.logs

    make_watchd(tmp_path, context)[0].run_once()
    assert read_state(tmp_path)["scanErrorStreak"] == 0


def test_a_state_file_of_another_version_starts_fresh(tmp_path: Path) -> None:
    (tmp_path / "watchd-state.json").write_text(json.dumps({"version": "old", "lastRun": 1}))
    watcher, _ = make_watchd(tmp_path)
    watcher.run_once()
    state = read_state(tmp_path)
    assert state["version"] == "voice-journey.watchd-state.v1"
    assert state["lastRun"] is None
    assert state["nextDigestAt"] > 1_000_000_000_000


def test_a_persisted_schedule_is_kept_and_recent_runs_are_capped(tmp_path: Path) -> None:
    runs = [{"ok": True, "signature": f"s{i}"} for i in range(10)]
    persisted = {
        "version": "voice-journey.watchd-state.v1",
        "lastRun": runs[-1],
        "recentRuns": runs,
        "lastDigest": None,
        "nextDigestAt": 5,
        "failure": None,
    }
    (tmp_path / "watchd-state.json").write_text(json.dumps(persisted))
    watcher, context = make_watchd(tmp_path, notify=False, ntfy_url=None)
    watcher.tick()
    assert watcher.tick()["status"] == "ran"
    state = read_state(tmp_path)
    assert state["nextDigestAt"] == 5
    assert len(state["recentRuns"]) == 10
    assert state["recentRuns"][0]["signature"] == "s1"
    assert state["recentRuns"][-1]["signature"] == "a:10"
    assert context.notices == []


def test_a_tick_during_a_tick_is_busy_and_stage_output_is_logged(tmp_path: Path) -> None:
    inner: list[dict[str, Any]] = []
    holder: list[Watchd] = []

    def run_stage(stage: Stage, context: StageContext) -> dict[str, str]:
        if stage.key == "index":
            inner.append(holder[0].tick())
            assert context.work_dir == str(tmp_path)
            assert context.python == sys.executable
            context.register_child(None)
            assert holder[0].current_child is None
        return {"stdoutTail": "written"}

    watcher, context = make_watchd(tmp_path, run_stage=run_stage)
    holder.append(watcher)
    watcher.tick()
    assert watcher.tick()["status"] == "ran"
    assert inner == [{"status": "busy"}]
    assert "stage index ok (0s)" in context.logs


def test_a_failing_ingest_hook_is_logged_and_a_stage_error_is_recorded(tmp_path: Path) -> None:
    def run_stage(stage: Stage, _: StageContext) -> dict[str, str]:
        if stage.key == "journeys":
            raise RuntimeError("no tail")
        return {}

    watcher, context = make_watchd(tmp_path, run_stage=run_stage)
    watcher.tick()
    assert watcher.tick()["status"] == "failed"
    last = read_state(tmp_path)["lastRun"]
    assert last["stages"][-1] == {"key": "journeys", "ok": False, "ms": 0, "error": "no tail"}
    assert list(last) == ["startedAt", "signature", "newCount", "ok", "stages", "endedAt"]
    assert not any("stderr tail" in line for line in context.logs)
    assert context.notices[-1]["body"].startswith("no tail — pipeline aborted after 7/7 stages")

    def broken_hook() -> None:
        raise RuntimeError("hook broke")

    hooked = Watchd(
        WatchOptions(
            corpus_root="/c",
            approval="ok",
            state_path=str(tmp_path / "other.json"),
            paths=resolve_paths({}),
        ),
        list_entries=lambda: [entry("z", 1)],
        run_stage=lambda stage, _: {},
        notify=lambda message: None,
        now=lambda: 0,
        on_ingested=broken_hook,
        log=context.logs.append,
    )
    hooked.tick()
    assert hooked.tick()["status"] == "ran"
    assert "on-ingested hook failed: hook broke" in context.logs
    assert hooked.work_dir == os.getcwd()


def test_lock_holders_that_are_not_a_live_other_process_are_reclaimed(tmp_path: Path) -> None:
    for index, holder in enumerate(
        [
            {"pid": os.getpid()},
            {"pid": 0},
            {"pid": "1"},
            {"pid": 1.5},
            {"pid": 2**70},
            {"other": True},
            [],
        ]
    ):
        root = tmp_path / str(index)
        root.mkdir()
        (root / "watchd-state.json.lock").write_text(json.dumps(holder))
        watcher, _ = make_watchd(root)
        watcher.tick()
        assert watcher.tick()["status"] == "ran", holder

    assert watch._pid_alive(float(os.getpid()))


def test_the_lock_stays_held_when_it_reappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watcher, context = make_watchd(tmp_path)
    monkeypatch.setattr(Watchd, "_create_lock", lambda self, payload: False)
    watcher.tick()
    assert watcher.tick()["status"] == "locked"
    assert "pipeline skipped: another run holds the lock" in context.logs


def test_the_lock_file_names_this_process_while_a_run_holds_it(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def run_stage(stage: Stage, _: StageContext) -> dict[str, str]:
        if stage.key == "index":
            seen.append(json.loads((tmp_path / "watchd-state.json.lock").read_text()))
        return {}

    watcher, _ = make_watchd(tmp_path, run_stage=run_stage)
    watcher.tick()
    watcher.tick()
    assert seen == [{"pid": os.getpid(), "startedAt": "2001-09-09T01:46:40.000Z"}]
    assert not (tmp_path / "watchd-state.json.lock").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


# ---- notifications ------------------------------------------------------------------------


def test_the_outbox_receives_one_whole_json_file_per_notification(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    watcher, context = make_watchd(tmp_path, notify=None, ntfy_url=None, notify_outbox=str(outbox))
    assert watcher.notifies
    watcher.tick()
    watcher.tick()
    files = sorted(outbox.iterdir())
    assert len(files) == 1
    assert files[0].name.startswith(f"1000000000000-{os.getpid()}-")
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    payload = json.loads(files[0].read_text())
    assert payload == {
        "schemaVersion": "voice-lab.journey-notification.v1",
        "createdAt": "2001-09-09T01:46:40.000Z",
        "title": "Voice Journey watchd: ingested",
        "body": "1 new take(s) processed through 7 stages in 0s.",
        "priority": "default",
        "tags": "white_check_mark",
    }

    context.clock += 9 * DAY_MS
    watcher.tick()
    assert len(list(outbox.iterdir())) == 2


def test_an_outbox_that_cannot_be_written_is_logged(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    watcher, context = make_watchd(
        tmp_path, notify=None, ntfy_url=None, notify_outbox=str(blocker / "outbox")
    )
    watcher.tick()
    assert watcher.tick()["status"] == "ran"
    assert any(line.startswith("notification outbox write failed") for line in context.logs)


def test_a_failed_outbox_rename_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(source: str, target: object) -> None:
        raise OSError("rename refused")

    monkeypatch.setattr(watch.os, "replace", refuse)
    with pytest.raises(OSError, match="rename refused"):
        write_outbox(str(tmp_path), {"title": "t", "body": "b"}, 0)
    assert list(tmp_path.iterdir()) == []


def test_without_a_channel_nothing_is_sent_and_no_digest_is_built(tmp_path: Path) -> None:
    watcher, context = make_watchd(tmp_path, notify=None, ntfy_url=None)
    assert not watcher.notifies
    watcher.tick()
    watcher.tick()
    context.clock += 9 * DAY_MS
    watcher.tick()
    assert read_state(tmp_path)["lastDigest"] is None


class FakeResponse:
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_ntfy_posts_title_priority_and_tags_and_logs_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests: list[tuple[urllib.request.Request, float]] = []

    def urlopen(request: urllib.request.Request, timeout: float) -> FakeResponse:
        requests.append((request, timeout))
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    outbox = tmp_path / "outbox"
    watcher, context = make_watchd(tmp_path, notify=None, notify_outbox=str(outbox))
    watcher.tick()
    watcher.tick()
    assert len(requests) == 1
    request, timeout = requests[0]
    assert timeout == 10.0
    assert request.full_url == "http://ntfy.test/topic"
    assert request.get_method() == "POST"
    assert request.data == b"1 new take(s) processed through 7 stages in 0s."
    assert request.get_header("Title") == "Voice Journey watchd: ingested"
    assert request.get_header("Priority") == "default"
    assert request.get_header("Tags") == "white_check_mark"
    assert len(list(outbox.iterdir())) == 1

    send_ntfy("http://ntfy.test/topic", {"title": "t", "body": "b"}, context.logs.append)
    assert requests[-1][0].get_header("Tags") is None

    def unreachable(request: urllib.request.Request, timeout: float) -> FakeResponse:
        raise OSError("no route")

    monkeypatch.setattr(urllib.request, "urlopen", unreachable)
    send_ntfy("http://ntfy.test/topic", {"title": "t", "body": "b"}, context.logs.append)
    assert "ntfy delivery failed: no route" in context.logs


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"corpus_root": ""}, "requires corpusRoot"),
        ({"approval": ""}, "operator approval"),
        ({"ntfy_url": "file:///etc/passwd"}, "http or https"),
    ],
)
def test_a_watcher_without_its_requirements_is_refused(
    tmp_path: Path, overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(WatchError, match=message):
        make_watchd(tmp_path, **overrides)


# ---- pure parts: JavaScript rules ---------------------------------------------------------


def test_signature_order_is_javascripts_default_string_order() -> None:
    entries = [entry("b", 1), entry("B", 1), entry("a", 10), entry("a", 9)]
    assert scan_signature(entries) == "B:1|a:10|a:9|b:1"
    assert scan_signature(None) == ""


def test_index_sizes_compare_strictly_and_fall_back_to_the_row_size() -> None:
    index = {"recordings": [{"recordingId": "a", "sizeBytes": 10}, {"recordingId": "b"}]}
    assert count_new_since_index([entry("a", 10)], index) == 0
    assert count_new_since_index([entry("b", 10)], index) == 1
    assert count_new_since_index([{"recordingId": "a", "sizeBytes": "10"}], index) == 1
    assert count_new_since_index(None, index) == 0
    with pytest.raises(TypeError, match="map is not a function"):
        count_new_since_index([], {"recordings": {}})


def test_next_digest_at_crosses_daylight_saving_changes_in_local_time() -> None:
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/New_York"
    time.tzset()
    try:
        autumn = local_ms(2026, 10, 31, 12, 0, 0)
        assert next_digest_at(autumn, "Sunday", 18) - autumn == 31 * 3_600_000
        spring = local_ms(2026, 3, 7, 12, 0, 0)
        assert next_digest_at(spring, "sunday", 18) - spring == 29 * 3_600_000
        skipped = local_ms(2026, 3, 1, 12, 0, 0)
        two_thirty = datetime.fromtimestamp(next_digest_at(skipped, "sunday", 2) / 1000)
        assert (two_thirty.month, two_thirty.day, two_thirty.hour) == (3, 8, 3)
    finally:
        if previous is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous
        time.tzset()
    with pytest.raises(ValueError, match="unknown digest day: someday"):
        next_digest_at(0, "someday")


def test_digest_counts_follow_javascripts_nullish_and_key_rules() -> None:
    snapshot = digest_snapshot_from_manifests(
        results={"results": [{"bucket": "noise"}, {"classification": None, "bucket": 2}, {}]},
        transcripts={"results": [{}]},
        journeys={"improvementIndex": []},
    )
    assert snapshot == {
        "rows": 3,
        "buckets": {"2": 1, "noise": 1, "unknown": 1},
        "transcribed": 1,
        "clusters": 0,
        "songJourneys": 0,
        "verdicts": {},
    }
    assert list(snapshot["buckets"]) == ["2", "noise", "unknown"]
    assert digest_snapshot_from_manifests(transcripts={"rows": "abc"})["transcribed"] == 3
    assert digest_snapshot_from_manifests(results={"results": "ab"})["buckets"] == {"unknown": 2}
    with pytest.raises(TypeError, match="reading 'classification'"):
        digest_snapshot_from_manifests(results={"results": [None]})
    with pytest.raises(TypeError, match="is not iterable"):
        digest_snapshot_from_manifests(results={"results": {}})


def test_digest_lines_sort_entries_as_strings_and_skip_unchanged_buckets() -> None:
    base = {"rows": 4, "transcribed": 2, "clusters": 1, "songJourneys": 0, "verdicts": {}}
    snapshot = {**base, "buckets": {"a": 1, "a+": 2, "b": 3}}
    assert digest_diff_lines(None, snapshot) == [
        "takes classified: 4",
        "transcribed: 2",
        "lyric clusters: 1",
        "song journeys: 0",
        "  a+: 2",
        "  a: 1",
        "  b: 3",
    ]
    previous = {**base, "rows": 6, "transcribed": 2, "buckets": {"a": 1, "b": 5}}
    assert digest_diff_lines(previous, snapshot) == [
        "takes classified: 4 (-2)",
        "transcribed: 2",
        "lyric clusters: 1",
        "song journeys: 0",
        "  a+: 2 (+2)",
        "  b: 3 (-2)",
    ]


# ---- the stage runner ---------------------------------------------------------------------


def fake_python(tmp_path: Path, body: str) -> str:
    script = tmp_path / "python"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def registered() -> Iterator[list[object]]:
    yield []


def test_a_stage_runs_as_a_python_module_in_the_work_directory(
    tmp_path: Path, registered: list[object]
) -> None:
    python = fake_python(tmp_path, 'pwd; echo "$@"')
    stage = Stage("index", "index", ("--corpus-root", "/c"), 60_000)
    result = default_run_stage(stage, StageContext(str(tmp_path), registered.append, python))
    assert result == {
        "stdoutTail": f"{tmp_path}\n-P -m cole_voice_lab.journey.index --corpus-root /c\n"
    }
    assert isinstance(registered[0], subprocess.Popen)
    assert registered[1] is None


def test_a_failing_stage_reports_its_exit_code_and_stderr_tail(tmp_path: Path) -> None:
    python = fake_python(tmp_path, "printf '%03000d' 0 >&2; echo end >&2; exit 3")
    stage = Stage("stt", "stt", (), 60_000)
    with pytest.raises(StageFailure, match="^stt: exit 3$") as failure:
        default_run_stage(stage, StageContext(str(tmp_path), lambda child: None, python))
    assert len(failure.value.stderr_tail) == 2000
    assert failure.value.stderr_tail.endswith("0end\n")


def test_a_stage_past_its_timeout_is_terminated_then_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = Stage("filter", "filter", (), 100)
    python = fake_python(tmp_path, "exec sleep 5")
    with pytest.raises(StageFailure, match="^filter: exit signal SIGTERM$"):
        default_run_stage(stage, StageContext(str(tmp_path), lambda child: None, python))

    monkeypatch.setattr(watch, "TERMINATE_GRACE_SECONDS", 0.2)
    stubborn = fake_python(tmp_path, "trap '' TERM; exec sleep 5")
    with pytest.raises(StageFailure, match="^filter: exit signal SIGKILL$"):
        default_run_stage(stage, StageContext(str(tmp_path), lambda child: None, stubborn))


def test_a_stage_that_cannot_start_reports_the_error_code(tmp_path: Path) -> None:
    stage = Stage("index", "index", (), 60_000)
    context = StageContext(str(tmp_path), lambda child: None, str(tmp_path / "missing"))
    with pytest.raises(StageFailure, match="^index: exit ENOENT$"):
        default_run_stage(stage, context)
    assert watch._exit_reason(-999) == "signal ?"


def test_a_real_stage_module_runs_under_this_interpreter(tmp_path: Path) -> None:
    stage = Stage("lyric", "lyrics", ("--help",), 60_000)
    result = default_run_stage(stage, StageContext(str(tmp_path), lambda child: None))
    assert "analyze" in result["stdoutTail"]


# ---- the loop, signals, status ------------------------------------------------------------


def test_the_loop_ticks_until_stopped_and_survives_a_failing_tick(tmp_path: Path) -> None:
    ticks: list[int] = []
    logs: list[str] = []
    state = tmp_path / "watchd-state.json"

    def list_entries() -> list[dict[str, Any]]:
        ticks.append(1)
        if len(ticks) == 2:
            state.write_text("not json")
            watcher._state = None
        if len(ticks) == 3:
            watcher.stop()
        return [entry("a", 10)]

    def log(line: str) -> None:
        logs.append(line)
        if line.startswith("tick failed:"):
            state.unlink()

    watcher = Watchd(
        WatchOptions(
            corpus_root="/fake-corpus",
            approval="ok",
            state_path=str(state),
            interval_seconds=0.01,
            paths=resolve_paths({}),
        ),
        list_entries=list_entries,
        run_stage=lambda stage, _: {},
        now=lambda: 0,
        log=log,
    )
    watcher.run_forever()
    assert logs[0] == "watching /fake-corpus every 0s"
    assert any(line.startswith("tick failed:") for line in logs)
    assert len(ticks) == 3


class FakeChild:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


def test_a_shutdown_signal_terminates_the_running_stage_and_exits(tmp_path: Path) -> None:
    watcher, context = make_watchd(tmp_path)
    child = FakeChild()
    watcher.current_child = child  # type: ignore[assignment]
    with pytest.raises(SystemExit) as stopped:
        watcher.handle_shutdown_signal(signal.SIGTERM)
    assert stopped.value.code == 143
    assert child.terminated
    assert "shutdown: terminating active pipeline stage" in context.logs
    watcher.current_child = None
    with pytest.raises(SystemExit) as interrupted:
        watcher.handle_shutdown_signal(signal.SIGINT)
    assert interrupted.value.code == 130


def test_a_shutdown_during_a_run_records_it_and_releases_the_lock(tmp_path: Path) -> None:
    def run_stage(stage: Stage, _: StageContext) -> dict[str, str]:
        if stage.key == "stt":
            raise SystemExit(143)
        return {}

    watcher, _ = make_watchd(tmp_path, run_stage=run_stage)
    watcher.tick()
    with pytest.raises(SystemExit):
        watcher.tick()
    last = read_state(tmp_path)["lastRun"]
    assert [stage["key"] for stage in last["stages"]] == ["index", "filter"]
    assert last["ok"] is False
    assert not (tmp_path / "watchd-state.json.lock").exists()


def test_the_status_snapshot_reports_configuration_and_runs(tmp_path: Path) -> None:
    watcher, _ = make_watchd(tmp_path, notify_outbox=str(tmp_path / "out"))
    assert watcher.status_snapshot()["nextDigestAt"] is None
    watcher.tick()
    watcher.tick()
    snapshot = watcher.status_snapshot()
    assert snapshot["enabled"] is True
    assert snapshot["intervalSeconds"] == 60
    assert snapshot["approvalRecorded"] is True
    assert snapshot["ntfy"] is True
    assert snapshot["notifyOutbox"] is True
    assert snapshot["lastTick"]["status"] == "ran"
    assert snapshot["lastRun"]["ok"] is True
    assert len(snapshot["recentRuns"]) == 1
    assert snapshot["nextDigestAt"].endswith("Z")
    assert snapshot["stages"] == PIPELINE_ORDER


# ---- the default listing and the command lines --------------------------------------------


def journey_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "journey"
    phone = data / "corpus" / "phone"
    phone.mkdir(parents=True)
    (phone / PHONE_A).write_bytes(b"0123456789")
    session = data / "corpus" / "measurement" / "sessionA1"
    session.mkdir(parents=True)
    (session / "seg-1.wav").write_bytes(b"0123")
    (session / "session.json").write_text(json.dumps({"files": [{"name": "seg-1.wav"}]}))
    for name in list(os.environ):
        if name.startswith("VOICE_JOURNEY_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("VOICE_JOURNEY_DATA", str(data))
    monkeypatch.setenv("VOICE_JOURNEY_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("VOICE_JOURNEY_APPROVAL", "approved by the owner")
    return data


def test_the_default_scan_lists_phone_and_measurement_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = journey_data(tmp_path, monkeypatch)
    calls: list[Stage] = []

    def run_stage(stage: Stage, _: StageContext) -> dict[str, str]:
        calls.append(stage)
        return {}

    watcher = Watchd(
        WatchOptions(corpus_root=str(data / "corpus" / "phone"), approval="approved"),
        run_stage=run_stage,
        notify=lambda message: None,
        now=lambda: 1_000_000_000_000,
        log=lambda line: None,
    )
    assert watcher.state_path == str(tmp_path / "state" / "watchd-state.json")
    assert watcher.tick()["status"] == "unstable"
    assert watcher.tick() == {"at": "2001-09-09T01:46:40.000Z", "status": "ran", "newCount": 2}
    assert calls[0].key == "index"
    assert calls[0].args[-2:] == ("--measurement-root", str(data / "corpus" / "measurement"))


def run_main(
    capsys: pytest.CaptureFixture[str], argv: list[str], entry_point: Callable[..., int]
) -> tuple[int, Any, str]:
    code = entry_point(argv)
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    return code, json.loads(lines[-1]) if lines else None, captured.err


def test_watch_once_from_the_command_line_uses_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = journey_data(tmp_path, monkeypatch)
    monkeypatch.setenv("VOICE_JOURNEY_CORPUS_ROOT", str(data / "corpus" / "phone"))
    seen: list[Stage] = []

    def run_stage(stage: Stage, context: StageContext) -> dict[str, str]:
        seen.append(stage)
        return {}

    monkeypatch.setattr(watch, "default_run_stage", run_stage)
    sigterm = signal.getsignal(signal.SIGTERM)
    argv = ["journey", "watch", "--once", "--notify-outbox", str(tmp_path / "outbox")]
    code, result, _ = run_main(capsys, argv, cli.main)
    assert (code, result["status"]) == (0, "unstable")
    code, result, _ = run_main(capsys, argv, cli.main)
    assert (code, result["status"], result["newCount"]) == (0, "ran", 2)
    assert signal.getsignal(signal.SIGTERM) == sigterm
    assert ("--approval", "approved by the owner") == seen[1].args[-2:]
    assert len(list((tmp_path / "outbox").iterdir())) == 1
    state = json.loads((tmp_path / "state" / "watchd-state.json").read_text())
    assert state["lastRun"]["ok"] is True


def test_watch_once_exits_1_for_a_failed_run_or_scan_and_a_broken_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    data = journey_data(tmp_path, monkeypatch)
    phone = str(data / "corpus" / "phone")

    def run_stage(stage: Stage, context: StageContext) -> dict[str, str]:
        raise StageFailure(f"{stage.key}: exit 1")

    monkeypatch.setattr(watch, "default_run_stage", run_stage)
    argv = ["--once", "--corpus-root", phone, "--approval", "flag approval"]
    code, result, _ = run_main(capsys, argv, watch.main)
    assert (code, result["status"]) == (0, "unstable")
    code, result, _ = run_main(capsys, argv, watch.main)
    assert (code, result["status"]) == (1, "failed")

    missing = ["--once", "--corpus-root", str(tmp_path / "none"), "--approval", "x"]
    code, result, err = run_main(capsys, missing, watch.main)
    assert (code, result["status"]) == (1, "scan-error")
    assert "scan failed" in err

    (tmp_path / "state" / "watchd-state.json").write_text("{")
    code, result, err = run_main(capsys, argv, watch.main)
    assert (code, result) == (1, None)
    assert "[watchd] tick failed:" in err


def test_watch_refuses_a_missing_approval_and_bad_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journey_data(tmp_path, monkeypatch)
    monkeypatch.setenv("VOICE_JOURNEY_APPROVAL", "")
    assert watch.main(["--once"]) == 2
    assert "operator approval" in capsys.readouterr().err
    for argv in (
        ["--interval", "0"],
        ["--interval", "x"],
        ["--digest-hour", "24"],
        ["--digest-hour", "x"],
        ["--digest-day", "someday"],
    ):
        with pytest.raises(SystemExit) as refused:
            watch.main(argv)
        assert refused.value.code == 2
    assert "voice-lab journey watch" in capsys.readouterr().err


def test_watch_without_once_runs_the_loop_with_the_parsed_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journey_data(tmp_path, monkeypatch)
    seen: list[Watchd] = []
    monkeypatch.setattr(Watchd, "run_forever", lambda self: seen.append(self))
    argv = [
        "--interval",
        "300",
        "--digest-day",
        "Monday",
        "--digest-hour",
        "7",
        "--ntfy",
        "https://ntfy.example/topic",
        "--index",
        "custom/index.json",
        "--state",
        str(tmp_path / "s.json"),
        "--measurement-root",
        str(tmp_path / "m"),
    ]
    assert watch.main(argv) == 0
    options = seen[0].options
    assert (options.interval_seconds, options.digest_day, options.digest_hour) == (300, "monday", 7)
    assert options.ntfy_url == "https://ntfy.example/topic"
    assert seen[0].manifest_paths["index"] == os.path.join(os.getcwd(), "custom/index.json")
    assert seen[0].state_path == str(tmp_path / "s.json")
    assert seen[0].measurement_root == str(tmp_path / "m")


@pytest.mark.parametrize(
    "command", ["index", "filter", "stt", "lyrics", "reclassify", "features", "journeys"]
)
def test_voice_lab_journey_forwards_every_argument_to_the_stage(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module(f"cole_voice_lab.journey.{command}")
    received: list[list[str]] = []
    monkeypatch.setattr(module, "main", lambda argv: received.append(argv) or 7)
    argv = ["sub", "--corpus-root", "/c", "--help", "-h", "--"]
    assert cli.main(["journey", command, *argv]) == 7
    assert received == [argv]


def test_voice_lab_journey_runs_a_real_stage_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["journey", "lyrics", "--help"]) == 0
    assert "analyze" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["journey"])
    assert sys.modules["cole_voice_lab.jobs.watch"] is watch
