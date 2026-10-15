"""Independently verify the canonical evidence behind native recovery.

Every assertion is re-derived from durable evidence through the canonical Python modules.
The harness reports are read only to name what to look up, never as the answer, so a
harness that reported the wrong outcome still fails here.
"""

from __future__ import annotations

import json
import math
import time
import wave
from pathlib import Path

from cole_voice_lab.canonical import write_canonical_json
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import NativeRecoveryReceipt
from cole_voice_lab.recording_recovery import RecordingRecovery, _read_native_receipt
from cole_voice_lab.recording_takes import RecordingMetadataBusyError, RecordingTakeStore

SUBJECT = "synthetic-subject"


def fixture_pcm(frames: int) -> bytes:
    """Recompute the fixture signal here; never trust a digest the harness printed."""

    period = b"".join(
        round(2_000_000 * math.sin(index * math.tau / 480)).to_bytes(3, "little", signed=True)
        for index in range(480)
    )
    return (period * ((frames + 479) // 480))[: frames * 3]


def read_pcm(path: Path) -> tuple[int, bytes]:
    with wave.open(str(path), "rb") as source:
        assert (
            source.getnchannels(),
            source.getsampwidth(),
            source.getframerate(),
        ) == (1, 3, 48000)
        frames = source.getnframes()
        return frames, source.readframes(frames)


def verify_zero_frame(directory: Path) -> dict[str, object]:
    """Stop before the first chunk: no seal or original, and the receipt stays untouched."""

    layout = AssetLayout.validate(directory / "assets")
    report = json.loads((directory / "native" / "recovery-zero-frame-report.json").read_text())
    assert report["ready_after_stop_ns"] <= report["ready_bound_ns"]
    assert report["recoverable_error_entered"] is False
    store = RecordingTakeStore(layout)
    take_id = report["take_id"]
    take = store.get_take(take_id)
    assert take is not None and take.seal is None, "A zero-frame Stop must not seal a take"
    take_root = layout.root / f"tmp/recording-takes/{take_id}"
    assert not (take_root / "original.wav").exists(), "A zero-frame Stop published an original"
    assert not (layout.root / f"provenance/recording-takes/{take_id}/seal.json").exists()
    receipt_path = take_root / "native-recovery.json"
    receipt_before = receipt_path.read_bytes()
    receipt = _read_native_receipt(receipt_path)
    assert receipt.take_id == take_id and receipt.state == "partial" and receipt.seal is None
    assert receipt.received_frames == 0 and receipt.written_frames == 0
    assert receipt.error is not None and receipt.error.code == "RECORDING.CANCELLED"

    # Re-derive the finding independently; classification never rewrites the receipt.
    status = RecordingRecovery(store, subject_id=SUBJECT).recover_take(take_id)
    assert status.status == "partial" and not status.recovered_seal, status
    assert status.error is not None and status.error.code == "RECORDING.CANCELLED"
    assert report["recovery_status"] == status.status
    assert report["recovery_error_code"] == status.error.code
    assert receipt_path.read_bytes() == receipt_before

    # Record stayed usable: the next capture in the same session sealed normally.
    follow_up = store.get_take(report["follow_up_take_id"])
    assert follow_up is not None and follow_up.seal is not None
    frames, pcm = read_pcm(layout.root / follow_up.seal.original_path)
    assert frames == follow_up.seal.audio.frames == 4801
    assert pcm == fixture_pcm(frames), "Follow-up original is not the exact fixture signal"
    evidence: dict[str, object] = {
        "zero_frame_take_id": take_id,
        "zero_frame_take_sealed": False,
        "zero_frame_final_original_present": False,
        "receipt_state": receipt.state,
        "receipt_error_code": receipt.error.code,
        "receipt_received_frames": receipt.received_frames,
        "independent_status": status.status,
        "independent_error_code": status.error.code,
        "ready_after_stop_ns": report["ready_after_stop_ns"],
        "ready_bound_ns": report["ready_bound_ns"],
        "follow_up_take_id": follow_up.reservation.take_id,
        "follow_up_frames": frames,
        "subject_id": SUBJECT,
    }
    write_canonical_json(directory / "native/python-zero-frame-evidence.json", evidence)
    return evidence


def _open_store(layout: AssetLayout, *, seconds: float = 10.0) -> RecordingTakeStore:
    """The take store, once the restarted app's service has finished its last commit: the
    report is written before the service exits, and the store refuses (never waits) while
    another process holds its metadata lock."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return RecordingTakeStore(layout)
        except RecordingMetadataBusyError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def verify(directory: Path) -> None:
    verify_zero_frame(directory / "zero-frame")
    layout = AssetLayout.validate(directory / "assets")
    crash = json.loads((directory / "native-crash" / "recovery-report.json").read_text())
    restart = json.loads(
        (directory / "native-restart" / "recovery-restart-report.json").read_text()
    )
    store = _open_store(layout)
    recovery = RecordingRecovery(store, subject_id=SUBJECT)

    failed = crash["take_id"]
    assert restart["recovered_partial_take_id"] == failed, (
        "The restarted session recovered a different take than the one that failed"
    )

    # A failed seal leaves no successful evidence of any kind.
    take = store.get_take(failed)
    assert take is not None and take.seal is None, "A failed seal must not publish a take seal"
    assert take.transport != "draft", take.transport
    take_root = layout.root / f"tmp/recording-takes/{failed}"
    assert not (take_root / "original.wav").exists(), "A failed seal published a final original"
    assert not (layout.root / f"provenance/recording-takes/{failed}/seal.json").exists()

    # Native receipts are ordinary framed JSON by contract, never canonical lab evidence,
    # so they are read through the same bounded private reader recovery itself uses.
    receipt = _read_native_receipt(take_root / "native-recovery.json")
    assert isinstance(receipt, NativeRecoveryReceipt)
    assert receipt.take_id == failed
    assert receipt.state == "partial", receipt.state
    assert receipt.error is not None and receipt.error.code == "RECORDING.WRITE_FAILED"

    # Recovery is re-run here independently of anything the app observed.
    status = recovery.recover_take(failed)
    assert status.status == "partial", status.status
    assert status.error is not None and status.error.code == "RECORDING.WRITE_FAILED"
    assert not status.recovered_seal
    page = recovery.page(limit=50)
    identities = [entry.take_id for entry in page.entries]
    assert len(identities) == len(set(identities)), "A recovery page repeated a take identity"
    assert failed in identities, "The bounded page omitted the failed capture"

    # The capture that followed recovery is an ordinary, complete, saved take.
    saved = store.get_take(restart["post_recovery_take_id"])
    assert saved is not None and saved.seal is not None
    assert saved.decision == "kept", saved.decision
    original = layout.root / saved.seal.original_path
    frames, pcm = read_pcm(original)
    assert frames == saved.seal.audio.frames == 4801
    assert pcm == fixture_pcm(frames), "Post-recovery original is not the exact fixture signal"
    assert saved.reservation.group.group_id == restart["restart_group_id"]
    # A restarted session records into its own context, never the failed one's group.
    assert take.reservation.group.group_id != saved.reservation.group.group_id
    # A changed acoustic setup opens a new group; history keeps its own recorded device.
    old_group = store.get_group(take.reservation.group.group_id)
    new_group = store.get_group(saved.reservation.group.group_id)
    assert old_group.capture_device.device_id == "synthetic-coreaudio-uid"
    assert new_group.capture_device.device_id == "synthetic-coreaudio-uid-changed"
    assert old_group.capture_host != new_group.capture_host

    # Sleep ended its capture without sealing it, and nothing resumed on its own.
    slept = restart["sleep_stopped_take_id"]
    assert slept not in {failed, restart["post_recovery_take_id"]}
    slept_take = store.get_take(slept)
    assert slept_take is not None and slept_take.seal is None, "Sleep must not seal a capture"
    assert slept_take.decision != "kept"

    # Another live owner is a terminal answer for that page, never a retry loop. `busy`
    # carries no error by contract: it is a statement about ownership, not about the take.
    busy = json.loads((directory / "native-busy" / "recovery-busy-report.json").read_text())
    assert busy["busy_entry_present"] is True
    # Both interrupted captures are incomplete, so either may be the reported row.
    assert busy["busy_take_id"] in {failed, slept}, "The busy result named an unknown take"
    # Once the owner is gone the same take resolves normally, so `busy` was about the owner
    # and never a permanent verdict on the recording itself.
    resolved = recovery.recover_take(busy["busy_take_id"])
    assert resolved.status != "busy", resolved.status

    evidence = {
        "failed_take_id": failed,
        "failed_take_sealed": False,
        "failed_take_final_original_present": False,
        "receipt_state": receipt.state,
        "receipt_error_code": receipt.error.code,
        "independent_status": status.status,
        "independent_error_code": status.error.code,
        "recovered_seal_claimed": status.recovered_seal,
        "page_take_count": len(identities),
        "page_identities_unique": True,
        "post_recovery_decision": saved.decision,
        "post_recovery_frames": frames,
        "restart_opened_new_group": True,
        "configuration_change_opened_new_group": True,
        "previous_group_device_preserved": old_group.capture_device.device_id,
        "restart_group_device": new_group.capture_device.device_id,
        "sleep_stopped_take_sealed": False,
        "busy_owner_reported_take": busy["busy_take_id"],
        "busy_resolved_after_owner_left": resolved.status,
        "subject_id": SUBJECT,
    }
    write_canonical_json(directory / "native-restart/python-recovery-evidence.json", evidence)
