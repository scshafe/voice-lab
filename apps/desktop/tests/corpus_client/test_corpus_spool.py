"""The corpus client's spool (D-052, RECORDING-V1.md step 5): a take is saved here first,
durably and privately; its package is built once and kept; the operator's changes are queued
with request ids made once and revisions chained; Restore goes back to the decision before."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

import pytest
from corpus_rig import PC, capture, client, record
from voice_lab_desktop.corpus.client import CorpusClient, PendingTake
from voice_lab_desktop.corpus.records import DecisionOp, LocalTake, TakeSync, TrimOp, UploadOp
from voice_lab_desktop.corpus.spool import (
    STATE_FILE,
    TAKE_FILE,
    CorpusSpool,
    SpoolError,
    capture_device,
    default_capture_host,
    default_client,
    restore_target,
)

from cole_voice_lab.canonical import write_canonical_json
from cole_voice_lab.corpus_takes import validate_take_package


@pytest.fixture
def corpus(pc: PC, data_dir: Path) -> CorpusClient:
    made = client(data_dir, pc)
    made.run_pass()
    return made


def prompt_ids(pc: PC) -> list[str]:
    return [prompt.prompt_id for prompt in pc.catalog[1].suite.prompts]


def test_a_saved_take_is_durable_private_and_packaged_once(
    pc: PC, corpus: CorpusClient, data_dir: Path
) -> None:
    pending = corpus.begin_take(prompt_ids(pc)[0])
    assert stat.S_IMODE(pending.directory.stat().st_mode) == 0o700
    assert list(pending.directory.iterdir()) == []
    captured = capture(pending.directory, seed=21)
    take = corpus.save_take(pending, captured)
    assert take.take_id == f"take-{captured.sha256}" and take.path.parent.name == "takes"
    assert not pending.directory.exists()
    assert sorted(path.name for path in take.path.iterdir()) == [
        "continuity.json",
        "original.wav",
        "package",
        "state.json",
        "take.json",
    ]
    for name in ("take.json", "state.json", "original.wav", "continuity.json"):
        assert stat.S_IMODE((take.path / name).stat().st_mode) == 0o600
    for directory in (take.path, take.path.parent, data_dir / "corpus"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    package = validate_take_package(take.package)
    assert package.manifest_sha256 == take.record.package_manifest_sha256
    assert package.manifest.capture_device.backend == "portaudio"
    assert package.manifest.client.version == "0.0.0-test"
    assert package.manifest.capture_host == "synthetic-host"
    assert package.manifest.requested_at == captured.requested_at
    sync = take.sync
    assert (sync.upload, sync.decision.decision, sync.decision.revision) == ("queued", "draft", 0)
    assert [op.kind for op in sync.outbox] == ["upload"]
    # The same audio cannot be saved twice.
    again = corpus.begin_take(prompt_ids(pc)[0])
    shutil.copy2(take.audio, again.directory / "original.wav")
    shutil.copy2(take.path / "continuity.json", again.directory / "continuity.json")
    duplicate = captured.__class__(
        path=again.directory / "original.wav",
        evidence_path=again.directory / "continuity.json",
        sha256=captured.sha256,
        size_bytes=captured.size_bytes,
        frames=captured.frames,
        requested_at=captured.requested_at,
        device=captured.device,
    )
    with pytest.raises(SpoolError, match="already saved"):
        corpus.save_take(again, duplicate)
    # A new spool over the same directory reads it back as it was.
    fresh = CorpusSpool(data_dir / "corpus")
    assert [t.take_id for t in fresh.takes()] == [take.take_id]
    assert fresh.take(take.take_id).sync == sync and fresh.damaged == []
    assert fresh.takes_for_prompt(prompt_ids(pc)[0])[0].take_id == take.take_id


def test_what_is_not_the_workers_take_is_not_saved(pc: PC, corpus: CorpusClient) -> None:
    pending = corpus.begin_take(prompt_ids(pc)[0])
    captured = capture(pending.directory, seed=22)
    elsewhere = PendingTake(pending.directory.parent.parent, pending.prompt, pending.catalog)
    with pytest.raises(SpoolError, match="not recorded into this spool"):
        corpus.save_take(elsewhere, captured)
    wrong = captured.__class__(**{**captured.__dict__, "frames": captured.frames - 1})
    with pytest.raises(SpoolError, match="not what its continuity evidence describes"):
        corpus.save_take(pending, wrong)
    os.chmod(captured.evidence_path, 0o644)
    with pytest.raises(SpoolError, match="cannot be saved: continuity.json is not private"):
        corpus.save_take(pending, captured)
    os.chmod(captured.evidence_path, 0o600)
    assert corpus.save_take(pending, captured).take_id == f"take-{captured.sha256}"
    with pytest.raises(SpoolError, match="not a recording directory"):
        corpus.spool.discard_recording(pending.directory.parent)


def test_a_take_that_cannot_be_packaged_stays_here_with_a_notice(
    pc: PC, corpus: CorpusClient
) -> None:
    pending = corpus.begin_take(prompt_ids(pc)[0])
    # Evidence from a device that is not a UMIK-1: no package the PC would take.
    take = corpus.save_take(
        pending, capture(pending.directory, seed=23, device_name="MacBook Air Microphone")
    )
    assert take.sync.upload == "local-only" and take.sync.outbox == ()
    assert take.record.local_only is not None and "not the required UMIK-1" in (
        take.record.local_only
    )
    assert not take.package.exists() and take.record.package_manifest_sha256 is None
    (notice,) = corpus.notices()
    assert notice.kind == "local-only" and notice.text.startswith("Kept on this machine only")
    # Its changes stay here too.
    corpus.decide(take.take_id, "kept")
    corpus.set_trim(take.take_id, 0, 480)
    sync = corpus.spool.take(take.take_id).sync
    assert (sync.decision.decision, sync.trim.end_frame, sync.outbox) == ("kept", 480, ())


def test_changes_queue_with_their_own_ids_and_chained_revisions(
    pc: PC, corpus: CorpusClient
) -> None:
    take = record(corpus, prompt_ids(pc)[1], seed=24)
    frames = take.record.frames
    corpus.decide(take.take_id, "kept")
    corpus.decide(take.take_id, "kept")  # no change: nothing queued
    corpus.set_trim(take.take_id, 100, frames - 100)
    first = corpus.spool.take(take.take_id).sync.changes
    # Unsent changes of a kind fold into one: its id and expected revision, the latest target.
    corpus.set_trim(take.take_id, 0, frames)  # the whole take: a reset
    corpus.decide(take.take_id, "rejected", "plosive")
    sync = corpus.spool.take(take.take_id).sync
    assert [(op.kind, op.request_id, op.expected_revision) for op in sync.changes] == [
        (op.kind, op.request_id, op.expected_revision) for op in first
    ]
    decision, trim = sync.changes
    assert isinstance(decision, DecisionOp) and isinstance(trim, TrimOp)
    assert (decision.decision, decision.reason, trim.start_frame, trim.end_frame) == (
        "rejected",
        "plosive",
        None,
        None,
    )
    body = decision.body()
    assert (body.request_id, body.reason, body.expected_revision) == (
        decision.request_id,
        "plosive",
        0,
    )
    assert trim.body().start_frame is None
    # A change the PC may know (tried, or being sent) is never altered: the next one chains.
    corpus.spool.tried(take.take_id, None, error="unreachable", retry_at=None)  # the upload
    upload, *_rest = corpus.spool.take(take.take_id).sync.outbox
    assert isinstance(upload, UploadOp) and upload.attempts == 1
    with corpus.spool.sending(decision.request_id):
        corpus.decide(take.take_id, "removed")
    corpus.restore(take.take_id)  # back to kept (the last kept or draft before): folded
    corpus.restore(take.take_id)  # not rejected or removed: nothing to restore
    sync = corpus.spool.take(take.take_id).sync
    assert [
        (op.kind, getattr(op, "decision", None), op.expected_revision) for op in sync.changes
    ] == [("decision", "rejected", 0), ("trim", None, 0), ("decision", "kept", 1)]
    assert len({op.request_id for op in sync.changes}) == 3
    assert all(op.request_id.startswith("req-") and len(op.request_id) == 36 for op in sync.changes)
    assert (sync.decision.decision, sync.decision.revision, sync.trim.start_frame) == (
        "kept",
        0,
        None,
    )
    # The rules of a change, before anything is queued.
    with pytest.raises(SpoolError, match="a rejection, and only a rejection, has a reason"):
        corpus.decide(take.take_id, "rejected")
    with pytest.raises(SpoolError, match="a rejection, and only a rejection, has a reason"):
        corpus.decide(take.take_id, "kept", "why")
    with pytest.raises(SpoolError, match="within the take's"):
        corpus.set_trim(take.take_id, 10, frames + 1)
    with pytest.raises(SpoolError, match="within the take's"):
        corpus.set_trim(take.take_id, 10, 10)
    with pytest.raises(SpoolError, match="both ends or neither"):
        corpus.set_trim(take.take_id, 10, None)
    with pytest.raises(SpoolError, match="not a take id"):
        corpus.decide("take-nope", "kept")
    with pytest.raises(SpoolError, match="no take"):
        corpus.decide("take-" + "0" * 64, "kept")


def test_restore_goes_back_to_the_last_kept_or_draft(pc: PC, corpus: CorpusClient) -> None:
    take = record(corpus, prompt_ids(pc)[2], seed=25)
    corpus.decide(take.take_id, "rejected", "noise")
    assert restore_target(corpus.spool.take(take.take_id).sync) == "draft"
    corpus.restore(take.take_id)
    corpus.decide(take.take_id, "kept")
    corpus.decide(take.take_id, "removed")
    corpus.decide(take.take_id, "rejected", "on second thought")
    assert corpus.restore(take.take_id).sync.decision.decision == "kept"


def test_an_interrupted_save_is_finished_and_damage_is_set_aside(
    pc: PC, corpus: CorpusClient, data_dir: Path
) -> None:
    kept = record(corpus, prompt_ids(pc)[0], seed=26)
    # A save interrupted after its records were written, before the rename.
    pending = corpus.begin_take(prompt_ids(pc)[1])
    captured = capture(pending.directory, seed=27)
    saved = corpus.save_take(pending, captured)
    os.rename(saved.path, pending.directory)
    # A save interrupted after the package was built: the package is reused, never rebuilt.
    second = corpus.begin_take(prompt_ids(pc)[2])
    captured_2 = capture(second.directory, seed=28)
    built = corpus.save_take(second, captured_2)
    manifest = built.record.package_manifest_sha256
    for name in (TAKE_FILE, STATE_FILE):
        (built.path / name).unlink()
    os.rename(built.path, second.directory)
    # A take directory whose records do not read.
    damaged = corpus.spool.take_dir("take-" + "1" * 64)
    damaged.mkdir(mode=0o700)
    (damaged / TAKE_FILE).write_text("{}")
    fresh = CorpusSpool(data_dir / "corpus")
    assert {t.take_id for t in fresh.takes()} == {kept.take_id, saved.take_id}
    assert fresh.damaged == [damaged.name]
    assert not pending.directory.exists() and second.directory.exists()
    again = client(data_dir, pc)  # a restart: the cached catalog is pinned
    rebuilt = again.save_take(second, captured_2)
    assert rebuilt.record.package_manifest_sha256 == manifest
    # A package beside the take that holds other audio is not used.
    third = again.begin_take(prompt_ids(pc)[3])
    captured_3 = capture(third.directory, seed=29)
    shutil.copytree(rebuilt.package, third.directory / "package")
    other = again.save_take(third, captured_3)
    assert other.sync.upload == "local-only" and "holds other audio" in (
        other.record.local_only or ""
    )


def test_the_records_hold_their_rules(pc: PC, corpus: CorpusClient) -> None:
    take = record(corpus, prompt_ids(pc)[0], seed=30)
    sync = take.sync
    with pytest.raises(ValueError, match="queued upload is the first operation"):
        TakeSync.model_validate({**dict(sync), "outbox": ()})
    with pytest.raises(ValueError, match="nothing is queued for a take that is never sent"):
        TakeSync.model_validate({**dict(sync), "upload": "local-only"})
    with pytest.raises(ValueError, match="has its receipt"):
        TakeSync.model_validate({**dict(sync), "upload": "accepted", "outbox": ()})
    with pytest.raises(ValueError, match="local-only, or has its package"):
        LocalTake.model_validate({**dict(take.record), "local_only": "why"})
    with pytest.raises(ValueError, match="a simulated take is never sent"):
        LocalTake.model_validate({**dict(take.record), "simulated": True})
    # A record that is not canonical is damage, not a take.
    write_canonical_json(take.path / STATE_FILE, sync)
    (take.path / STATE_FILE).write_text((take.path / STATE_FILE).read_text() + " ")
    fresh = CorpusSpool(corpus.root)
    assert fresh.takes() == [] and fresh.damaged == [take.take_id]


def test_the_take_names_this_app_and_its_device(monkeypatch: Any) -> None:
    import platform

    monkeypatch.setattr(platform, "node", lambda: "")
    assert default_capture_host() == "unknown-host"
    assert default_client().name == "voice-lab-desktop"
    device = capture_device("Umik-1  Gain: 18dB [Core Audio]")
    assert device.backend == "portaudio" and "UMIK-1" in device.display_name
