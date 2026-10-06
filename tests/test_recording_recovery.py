from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import macos_only
import pytest
from test_recording_takes import Rig
from test_recording_takes import rig as rig

from cole_voice_lab.canonical import canonical_sha256, sha256_file, write_canonical_json
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import CaptureDevice, utc_now
from cole_voice_lab.recording_analysis import RecordingAnalysisQueue, RecordingAnalysisWorker
from cole_voice_lab.recording_contracts import (
    CatalogSnapshot,
    NativeRecoveryReceipt,
    OpenContextCommand,
    RecordingError,
)
from cole_voice_lab.recording_index import RecordingIndex
from cole_voice_lab.recording_library import DesktopSessionEvidence
from cole_voice_lab.recording_ownership import capture_ownership
from cole_voice_lab.recording_recovery import ContinueLegacyRecordingCommand, RecordingRecovery


def emergency(rig: Rig, *, partial: bool = False) -> tuple[str, Path]:
    reservation = rig.reserve()
    seal = rig.receipt(reservation)
    receipt = NativeRecoveryReceipt(
        take_id=reservation.take_id,
        reservation_sha256=canonical_sha256(reservation),
        process_generation=reservation.process_generation,
        recorded_at=rig.now,
        partial_path=reservation.partial_path,
        received_frames=seal.audio.frames,
        written_frames=seal.audio.frames,
        state="partial" if partial else "sealed",
        seal=None if partial else seal,
        error=RecordingError(
            code="RECORDING.WRITE_FAILED", message="Synthetic interruption", retryable=False
        )
        if partial
        else None,
    )
    path = rig.layout.root / f"tmp/recording-takes/{reservation.take_id}/native-recovery.json"
    path.write_text(receipt.model_dump_json(indent=2))
    path.chmod(0o600)
    return reservation.take_id, path


def test_sealed_receipt_recovers_unkept_draft_and_replays(rig: Rig) -> None:
    take_id, path = emergency(rig)
    before = path.read_bytes()
    original = path.parent / "original.wav"
    digest = sha256_file(original)
    submitted: list[str] = []
    recovery = RecordingRecovery(rig.store, submit_analysis=submitted.append)
    result = recovery.recover_take(take_id)
    assert result.status == "draft" and result.recovered_seal
    take = rig.store.get_take(take_id)
    assert take.analysis == "pending" and take.decision == "draft"
    assert take.export == "unprepared" and not take.satisfies_coverage
    revision = rig.store.state_revision
    assert recovery.recover_take(take_id).recovered_seal is False
    assert rig.store.state_revision == revision and submitted == [take_id, take_id]
    assert path.read_bytes() == before and sha256_file(original) == digest


def test_partial_never_becomes_successful_original(rig: Rig) -> None:
    take_id, path = emergency(rig, partial=True)
    # A failed directory sync can leave a final-named WAV. No successful receipt
    # means it remains partial, even when the bytes could pass format inspection.
    original = path.parent / "original.wav"
    digest = sha256_file(original)
    result = RecordingRecovery(rig.store).recover_take(take_id)
    assert result.status == "partial" and result.error is not None
    assert rig.store.get_take(take_id).seal is None
    assert sha256_file(original) == digest


@pytest.mark.parametrize(
    "change", ["duplicate", "foreign-reservation", "public", "symlink", "oversize"]
)
def test_untrusted_receipt_cannot_publish_seal(rig: Rig, change: str) -> None:
    take_id, path = emergency(rig)
    if change == "duplicate":
        path.write_text('{"take_id":"foreign",' + path.read_text()[1:])
    elif change == "foreign-reservation":
        data = json.loads(path.read_text())
        data["reservation_sha256"] = "0" * 64
        path.write_text(json.dumps(data))
    elif change == "public":
        path.chmod(0o644)
    elif change == "symlink":
        target = path.with_suffix(".saved")
        path.rename(target)
        path.symlink_to(target)
    else:
        with path.open("ab") as stream:
            stream.truncate(1_048_577)
    assert RecordingRecovery(rig.store).recover_take(take_id).status == "unavailable"
    assert rig.store.get_take(take_id).seal is None


def test_active_owner_defers_recovery_without_mutation(rig: Rig) -> None:
    take_id, _ = emergency(rig)
    with capture_ownership(rig.layout):
        assert RecordingRecovery(rig.store).recover_take(take_id).status == "busy"
        assert rig.store.get_take(take_id).seal is None
    assert RecordingRecovery(rig.store).recover_take(take_id).status == "draft"


def test_missing_receipt_preserves_incomplete_audio(rig: Rig) -> None:
    take_id, path = emergency(rig)
    path.unlink()
    assert RecordingRecovery(rig.store).recover_take(take_id).status == "partial"
    (path.parent / "original.wav").unlink()
    assert RecordingRecovery(rig.store).recover_take(take_id).status == "empty"


def test_pages_are_bounded_and_metadata_only_for_registered_takes(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    take_id, _ = emergency(rig)
    recovery = RecordingRecovery(rig.store)
    recovery.recover_take(take_id)
    real_open = os.open

    def checked_open(path: str | Path, flags: int, mode: int = 0o777, **kw: object) -> int:
        assert not str(path).endswith(".wav")
        return real_open(path, flags, mode, **kw)

    monkeypatch.setattr(os, "open", checked_open)
    first = recovery.page(limit=1)
    assert len(first.entries) == 1 and first.next_take_id == take_id
    assert not recovery.page(after_take_id=take_id).entries
    with pytest.raises(ValueError):
        recovery.page(limit=51)


def legacy_command(rig: Rig) -> tuple[ContinueLegacyRecordingCommand, Path]:
    group = rig.store.get_group(rig.group_id)
    legacy = DesktopSessionEvidence(
        session_id="legacy-unfinished",
        subject_id=group.subject_id,
        catalog_suite=group.catalog,
        catalog_sha256=group.catalog_sha256,
        selected_suite=group.catalog,
        session_started_at=rig.now,
        capture_device=CaptureDevice(
            backend="avfoundation", device_id="old-input", display_name="Old"
        ),
        capture_host="old-host",
    )
    path = rig.layout.root / "provenance/capture-desktop/legacy-unfinished.json"
    digest = write_canonical_json(path, legacy)
    command = ContinueLegacyRecordingCommand(
        operation_id="continue-legacy",
        session_id=legacy.session_id,
        session_evidence_sha256=digest,
        context=OpenContextCommand(
            operation_id="continue-legacy",
            subject_id=group.subject_id,
            catalog=group.catalog,
            catalog_sha256=group.catalog_sha256,
            capture_device=group.capture_device,
            capture_host=group.capture_host,
            new_setup=True,
        ),
    )
    return command, path


def test_legacy_continuation_preserves_old_device_and_provenance(rig: Rig) -> None:
    command, path = legacy_command(rig)
    before = path.read_bytes()
    recovery = RecordingRecovery(rig.store)
    context = recovery.continue_legacy(command)
    assert context.group.group_id != rig.group_id
    assert context.group.capture_device.backend == "coreaudio"
    assert context.group.capture_host == "host"
    assert path.read_bytes() == before
    revision = rig.store.state_revision
    assert recovery.continue_legacy(command) == context
    assert rig.store.state_revision == revision
    assert not rig.store.iter_takes()
    frozen = rig.layout.root / "provenance/recording-legacy-continuations/continue-legacy.json"
    assert (
        json.loads(frozen.read_bytes())["original_evidence"]["capture_device"]["backend"]
        == "avfoundation"
    )


def test_legacy_intent_survives_context_commit_failure(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    command, path = legacy_command(rig)
    before = path.read_bytes()
    recovery = RecordingRecovery(rig.store)

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("synthetic context failure after intent")

    with monkeypatch.context() as patch:
        patch.setattr(rig.store, "open_context", fail)
        with pytest.raises(OSError, match="synthetic"):
            recovery.continue_legacy(command)
    assert rig.store.get_cursor(command.context.subject_id) is None
    intent = rig.layout.root / "provenance/recording-legacy-continuations/continue-legacy.json"
    assert intent.is_file()
    result = recovery.continue_legacy(command)
    assert result.group.reason == "new-setup" and path.read_bytes() == before


@pytest.mark.parametrize(
    "point",
    [
        "after-seal",
        "after-intent",
        "after-operation",
        "after-take-head",
        "after-store-head",
        "after-projection",
        "before-ack",
    ],
)
def test_seal_recovery_retries_every_durable_commit_boundary(rig: Rig, point: str) -> None:
    take_id, path = emergency(rig)
    original = path.parent / "original.wav"
    before = sha256_file(original)

    def fail(current: str) -> None:
        if current == point:
            raise OSError("synthetic filesystem interruption")

    rig.store.fault_hook = fail
    recovery = RecordingRecovery(rig.store)
    result = recovery.recover_take(take_id)
    assert result.status == "unavailable"
    rig.store.fault_hook = None
    assert recovery.recover_take(take_id).status == "draft"
    assert rig.store.get_take(take_id).decision == "draft"
    assert sha256_file(original) == before


@pytest.mark.parametrize(
    "point", ["after-seal", "after-operation", "after-take-head", "after-store-head"]
)
def test_owned_process_death_recovers_receipt_without_keeping(rig: Rig, point: str) -> None:
    take_id, path = emergency(rig)
    original = path.parent / "original.wav"
    before = sha256_file(original)
    process = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            """
import os, signal, sys
from pathlib import Path
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_recovery import RecordingRecovery
from cole_voice_lab.recording_takes import RecordingTakeStore
def fault(boundary):
    if boundary == sys.argv[3]:
        os.kill(os.getpid(), signal.SIGKILL)
store = RecordingTakeStore(AssetLayout.validate(Path(sys.argv[1])), fault_hook=fault)
RecordingRecovery(store).recover_take(sys.argv[2])
""",
            str(rig.layout.root),
            take_id,
            point,
        ],
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == -signal.SIGKILL
    assert RecordingRecovery(rig.store).recover_take(take_id).status == "draft"
    assert rig.store.get_take(take_id).decision == "draft"
    assert sha256_file(original) == before


@pytest.mark.parametrize("changed", ["digest", "subject", "finalized", "replay-command"])
def test_legacy_continuation_rejects_changed_or_completed_source(rig: Rig, changed: str) -> None:
    command, path = legacy_command(rig)
    before = path.read_bytes()
    recovery = RecordingRecovery(rig.store)
    if changed == "digest":
        command = command.model_copy(update={"session_evidence_sha256": "0" * 64})
    elif changed == "subject":
        command = command.model_copy(
            update={"context": command.context.model_copy(update={"subject_id": "someone-else"})}
        )
    elif changed == "finalized":
        ready = rig.layout.root / "tmp/capture-legacy-unfinished/READY"
        ready.parent.mkdir(mode=0o700)
        ready.write_bytes(b"")
        ready.chmod(0o600)
    else:
        recovery.continue_legacy(command)
        command = command.model_copy(
            update={"context": command.context.model_copy(update={"capture_host": "changed-host"})}
        )
    with pytest.raises((ContractError, SecurityError, ValueError)):
        recovery.continue_legacy(command)
    assert path.read_bytes() == before


def test_one_corrupt_take_does_not_prevent_other_receipt_recovery(rig: Rig) -> None:
    good_id, _ = emergency(rig)
    bad = rig.reserve("reserve-bad")
    head = rig.layout.root / f"provenance/recording-takes/{bad.take_id}/head.json"
    head.write_bytes(b"{broken\n")
    report = RecordingRecovery(rig.store).page()
    statuses = {entry.take_id: entry.status for entry in report.entries}
    assert statuses == {good_id: "draft", bad.take_id: "unavailable"}


@macos_only.REAL_RECORDING_SERVICE
def test_recovered_seal_is_not_independent_verification_after_preserved_mtime_tamper(
    rig: Rig,
) -> None:
    take_id, receipt_path = emergency(rig)
    path = receipt_path.parent / "original.wav"
    metadata = path.stat()
    changed = bytearray(path.read_bytes())
    changed[60] ^= 127
    path.write_bytes(changed)
    os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    recovery = RecordingRecovery(rig.store)
    assert recovery.recover_take(take_id).status == "draft"
    assert rig.store.get_take(take_id).analysis == "pending"
    rig.store.clock = utc_now
    with RecordingIndex(rig.layout, subject_id="synthetic-subject") as index:
        index.register_catalog(CatalogSnapshot.embedded(rig.store.get_group(rig.group_id).catalog))
        queue = RecordingAnalysisQueue(rig.store, index)
        worker = RecordingAnalysisWorker(rig.layout, queue.build)
        try:
            queue.submit(take_id)
            job = queue.claim()
            assert job is not None
            publication = worker.analyze(job)
            assert publication.result.state == "invalid"
            queue.complete(publication)
            assert rig.store.get_take(take_id).analysis == "invalid"
            assert not rig.store.get_take(take_id).satisfies_coverage
        finally:
            worker.close()
            queue.close()


def test_cli_continues_an_unfinished_desktop_session_from_its_own_evidence(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    """The operator names only the new setup; subject and catalog come from frozen evidence."""

    from cole_voice_lab.cli import main as cli_main

    _, path = legacy_command(rig)
    before = path.read_bytes()
    exit_code = cli_main(
        [
            "recording",
            "continue-legacy",
            "--asset-root",
            str(rig.layout.root),
            "--session-id",
            "legacy-unfinished",
            "--device-id",
            "new-coreaudio-uid",
            "--display-name",
            "UMIK-1",
            "--capture-host",
            "new-host",
        ]
    )
    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["continued_session_id"] == "legacy-unfinished"
    assert result["subject_id"] == rig.store.get_group(rig.group_id).subject_id
    # A continuation opens its own context and never rewrites the old evidence.
    assert result["group_id"] != rig.group_id
    assert path.read_bytes() == before
    group = rig.store.get_group(result["group_id"])
    assert group.capture_device.backend == "coreaudio"
    assert group.capture_device.device_id == "new-coreaudio-uid"
    assert group.capture_host == "new-host"


def test_cli_refuses_to_continue_a_finalized_desktop_session(
    rig: Rig, capsys: pytest.CaptureFixture[str]
) -> None:
    from cole_voice_lab.cli import main as cli_main

    _, path = legacy_command(rig)
    evidence = DesktopSessionEvidence.model_validate_json(path.read_bytes())
    finalized = evidence.model_copy(update={"manifest_sha256": "a" * 64})
    write_canonical_json(path, finalized)
    before = rig.store.state_revision
    assert (
        cli_main(
            [
                "recording",
                "continue-legacy",
                "--asset-root",
                str(rig.layout.root),
                "--session-id",
                "legacy-unfinished",
                "--device-id",
                "new-coreaudio-uid",
                "--display-name",
                "UMIK-1",
                "--capture-host",
                "new-host",
            ]
        )
        != 0
    )
    assert "already finalized" in capsys.readouterr().err
    assert rig.store.state_revision == before
