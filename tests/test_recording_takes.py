from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from recording_fixtures import synthetic_catalog, write_synthetic_wav
from test_recording_contracts import synthetic_contracts

from cole_voice_lab.canonical import (
    canonical_sha256,
    sha256_file,
    write_canonical_json,
)
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import CaptureDevice, PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    CaptureReservation,
    CaptureSealReceipt,
    RecordingFrameRange,
    RecordingNavigationResult,
    RecordingSelection,
    RecordingTake,
    SaveTakeCommand,
    ServiceRequest,
)
from cole_voice_lab.recording_service import (
    RecordingCancellation,
    RecordingServiceError,
    RecordingTakesBackend,
)
from cole_voice_lab.recording_takes import (
    RecordingMetadataBusyError,
    RecordingTakeOperation,
    RecordingTakeStore,
)

NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)
DEVICE = CaptureDevice(backend="coreaudio", device_id="synthetic", display_name="Synthetic")


@dataclass
class Rig:
    layout: AssetLayout
    store: RecordingTakeStore
    group_id: str
    now: datetime = NOW

    def reserve(
        self, operation: str = "reserve-1", prompt: str = "synthetic-0000"
    ) -> CaptureReservation:
        return self.store.reserve(
            operation_id=operation,
            group_id=self.group_id,
            prompt_id=prompt,
            process_generation="helper-generation",
        )

    def seal(self, reservation: CaptureReservation, operation: str = "seal-1") -> RecordingTake:
        receipt = self.receipt(reservation)
        return self.store.register_seal(operation_id=operation, receipt=receipt)

    def receipt(self, reservation: CaptureReservation) -> CaptureSealReceipt:
        path = self.layout.root / reservation.original_path
        if not path.exists():
            write_synthetic_wav(path, 0.01)
        digest, size = sha256_file(path)
        base = synthetic_contracts()["capture-seal"].model_dump()
        frames = 480
        base.update(
            take_id=reservation.take_id,
            reservation_sha256=canonical_sha256(reservation),
            process_generation=reservation.process_generation,
            original_path=reservation.original_path,
            original_sha256=digest,
            original_size_bytes=size,
            requested_at=reservation.requested_at,
            first_sample_at=reservation.requested_at,
            last_sample_at=reservation.requested_at + timedelta(seconds=0.01),
            sealed_at=reservation.requested_at + timedelta(seconds=1),
        )
        base["audio"].update(frames=frames, duration_seconds=0.01)
        base["continuity"].update(
            device_uid=DEVICE.device_id,
            frames=frames,
            received_frames=frames,
            first_sample_time=100.0,
            last_sample_end=580.0,
            wav_sha256=digest,
            wav_size_bytes=size,
        )
        self.now = reservation.requested_at + timedelta(seconds=2)
        return CaptureSealReceipt.model_validate(base)

    def command(
        self, take: RecordingTake, operation: str = "save-1", **changes: Any
    ) -> SaveTakeCommand:
        return SaveTakeCommand.model_validate(
            dict(
                operation_id=operation,
                selection=RecordingSelection(
                    catalog_sha256=take.reservation.group.catalog_sha256,
                    prompt_id=take.reservation.prompt.prompt_id,
                    take_id=take.reservation.take_id,
                    selection_revision=1,
                    index_revision=0,
                ),
                expected_decision_revision=take.decision_revision,
                expected_edit_revision=take.edit_revision,
                frame_range=take.frame_range,
                advance=False,
                waveform_ready=True,
            )
            | changes
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    result = Rig(layout, RecordingTakeStore(layout), "")
    result.store = RecordingTakeStore(layout, clock=lambda: result.now)
    result.group_id = result.store.open_group(
        synthetic_catalog(3), "synthetic-subject", DEVICE, "host"
    ).group_id
    return result


def reopen(rig: Rig, **options: Any) -> RecordingTakeStore:
    return RecordingTakeStore(rig.layout, clock=lambda: rig.now, **options)


def test_durable_reserve_seal_save_and_exact_replay(rig: Rig) -> None:
    reservation = rig.reserve()
    assert rig.store.state_revision == 1
    assert rig.store.reservation_result("reserve-1").seal is None
    take = rig.seal(reservation)
    assert take.decision == "draft" and take.analysis == "pending" and take.export == "unprepared"
    assert rig.store.state_revision == 2
    assert reopen(rig).get_take(reservation.take_id) == take
    command = rig.command(take)
    saved = rig.store.save(command)
    assert saved.take.decision == "kept" and saved.take.analysis == "pending"
    assert saved.take.satisfies_coverage
    assert rig.store.state_revision == 3
    assert reopen(rig).save(command) == saved
    assert rig.reserve() == reservation
    assert rig.store.get_take(reservation.take_id) == saved.take
    assert rig.store.reservation_result("reserve-1").seal is None
    assert len(list((rig.layout.root / "provenance/recording-takes/.operations").iterdir())) == 3
    assert not list((rig.layout.root / "raw").iterdir())
    for path in (rig.layout.root / "provenance").rglob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_new_draft_and_explicit_save_are_independent(rig: Rig) -> None:
    one = rig.seal(rig.reserve())
    two = rig.seal(rig.reserve("reserve-2", "synthetic-0001"), "seal-2")
    saved = rig.store.save(rig.command(two))
    assert rig.store.get_take(one.reservation.take_id).decision == "draft"
    assert saved.take.reservation.take_id != one.reservation.take_id
    assert len(rig.store.iter_takes()) == 2


def test_automatic_groups_rotate_without_a_session_form(rig: Rig) -> None:
    group = rig.store.get_group(rig.group_id)
    assert rig.store.open_group(group.catalog, group.subject_id, DEVICE, "host") == group
    changed = rig.store.open_group(
        group.catalog,
        group.subject_id,
        DEVICE.model_copy(update={"device_id": "synthetic-2"}),
        "host",
    )
    assert changed.reason == "device-change" and changed.group_id != group.group_id
    setup = rig.store.open_group(group.catalog, group.subject_id, DEVICE, "host", new_setup=True)
    assert setup.reason == "new-setup"
    config = rig.store.open_group(group.catalog, group.subject_id, DEVICE, "other-host")
    assert config.reason == "configuration-change"
    started = reopen(rig).open_group(group.catalog, group.subject_id, DEVICE, "host")
    assert started.reason == "app-start" and started.group_id != group.group_id


def test_every_group_mint_refuses_physical_test_catalogs(rig: Rig) -> None:
    from recording_fixtures import synthetic_physical_test_catalog

    groups = sorted((rig.layout.root / "provenance").rglob("group-*.json"))
    current = rig.store.get_group(rig.group_id)
    for new_setup in (False, True):
        with pytest.raises(ContractError, match="physical-test catalogs"):
            rig.store.open_group(
                synthetic_physical_test_catalog(),
                "synthetic-subject",
                DEVICE,
                "host",
                new_setup=new_setup,
            )
    assert sorted((rig.layout.root / "provenance").rglob("group-*.json")) == groups
    assert rig.store.get_group(rig.group_id) == current


def test_corpus_gate_unprepared_disables_reserve_but_holdout_stays_bound(rig: Rig) -> None:
    catalog = synthetic_catalog(2)
    corpus = PromptSuite.model_validate(catalog.model_dump() | {"purpose": "capture-corpus"})
    group = rig.store.open_group(corpus, "synthetic-subject", DEVICE, "host")
    with pytest.raises(ContractError, match="not prepared"):
        rig.store.reserve(
            operation_id="not-ready",
            group_id=group.group_id,
            prompt_id=corpus.prompts[0].prompt_id,
            process_generation="helper",
        )
    heldout = PromptSuite.model_validate(
        catalog.model_dump()
        | {
            "purpose": "held-out-evaluation",
            "prompts": tuple(p.model_dump() | {"holdout": True} for p in catalog.prompts),
        }
    )
    group = rig.store.open_group(heldout, "synthetic-subject", DEVICE, "host")
    reserved = rig.store.reserve(
        operation_id="heldout",
        group_id=group.group_id,
        prompt_id=heldout.prompts[0].prompt_id,
        process_generation="helper",
    )
    assert reserved.prompt.holdout


@pytest.mark.parametrize(
    "boundary",
    [
        "before-reservation",
        "after-reservation",
        "before-intent",
        "after-intent",
        "before-operation",
        "after-operation",
        "before-take-head",
        "after-take-head",
        "before-store-head",
        "after-store-head",
        "before-projection",
        "after-projection",
        "before-completion",
        "after-completion",
        "before-ack",
    ],
)
def test_reservation_crash_and_retry_never_duplicates_or_keeps(rig: Rig, boundary: str) -> None:
    def fail(at: str) -> None:
        if at == boundary:
            raise OSError("synthetic injected write interruption")

    rig.store.fault_hook = fail
    with pytest.raises(OSError):
        rig.reserve()
    rig.store = reopen(rig)
    reservation = rig.reserve()
    assert rig.store.get_take(reservation.take_id).decision == "draft"
    assert len(rig.store.iter_takes()) == 1
    assert rig.store.state_revision == 1


@pytest.mark.parametrize(
    "boundary",
    [
        "before-seal",
        "after-seal",
        "before-intent",
        "after-intent",
        "before-operation",
        "after-operation",
        "before-take-head",
        "after-take-head",
        "before-store-head",
        "after-store-head",
        "before-projection",
        "after-projection",
        "before-completion",
        "after-completion",
        "before-ack",
    ],
)
def test_registration_crash_and_retry_preserves_only_a_draft(rig: Rig, boundary: str) -> None:
    reservation = rig.reserve()
    receipt = rig.receipt(reservation)

    def fail(at: str) -> None:
        if at == boundary:
            raise OSError("synthetic injected seal interruption")

    rig.store.fault_hook = fail
    with pytest.raises(OSError):
        rig.store.register_seal(operation_id="seal-1", receipt=receipt)
    rig.store = reopen(rig)
    take = rig.store.register_seal(operation_id="seal-1", receipt=receipt)
    assert take.decision == "draft" and take.analysis == "pending"
    assert rig.store.state_revision == 2


@pytest.mark.parametrize(
    "boundary",
    [
        "before-intent",
        "after-intent",
        "before-operation",
        "after-operation",
        "before-take-head",
        "after-take-head",
        "before-store-head",
        "after-store-head",
        "before-projection",
        "after-projection",
        "before-completion",
        "after-completion",
        "before-ack",
    ],
)
def test_save_crash_ordering_and_retry(rig: Rig, boundary: str) -> None:
    take = rig.seal(rig.reserve())
    command = rig.command(take)

    def fail(at: str) -> None:
        if at == boundary:
            raise OSError("synthetic injected Save interruption")

    rig.store.fault_hook = fail
    with pytest.raises(OSError):
        rig.store.save(command)
    store = reopen(rig)
    head = store.get_take(take.reservation.take_id)
    committed = boundary not in {"before-intent", "after-intent", "before-operation"}
    assert (head.decision == "kept") is committed
    saved = store.save(command)
    assert saved.take.decision_revision == 1
    assert store.state_revision == 3
    assert len(store.iter_takes()) == 1


@pytest.mark.parametrize("ordinal", range(1, 10))
def test_every_save_file_and_directory_fsync_failure_prevents_ack_and_retries(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, ordinal: int
) -> None:
    take = rig.seal(rig.reserve())
    command = rig.command(take)
    original = os.fsync
    calls = 0

    def fail(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == ordinal:
            raise OSError("synthetic fsync failure")
        original(descriptor)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail)
        with pytest.raises(OSError):
            rig.store.save(command)
    saved = reopen(rig).save(command)
    assert saved.take.decision == "kept" and saved.take.decision_revision == 1


@pytest.mark.parametrize("ordinal", range(1, 5))
def test_every_save_rename_failure_prevents_ack_and_retries(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, ordinal: int
) -> None:
    take = rig.seal(rig.reserve())
    command = rig.command(take)
    original = os.replace
    calls = 0

    def fail(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == ordinal:
            raise OSError("synthetic rename failure")
        original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail)
        with pytest.raises(OSError):
            rig.store.save(command)
    assert reopen(rig).save(command).take.decision_revision == 1


def test_projection_failure_has_durable_commit_and_repaired_head_before_ack(rig: Rig) -> None:
    take = rig.seal(rig.reserve())

    def broken(_take: RecordingTake) -> None:
        raise OSError("synthetic index unavailable")

    rig.store.projection_hook = broken
    command = rig.command(take)
    with pytest.raises(OSError):
        rig.store.save(command)
    projected: list[RecordingTake] = []
    store = reopen(rig, projection_hook=projected.append)
    saved = store.save(command)
    assert projected[-1] == saved.take == store.get_take(take.reservation.take_id)


def test_save_navigation_is_persisted_before_ack_and_ignores_unrelated_index_revision(
    rig: Rig,
) -> None:
    take = rig.seal(rig.reserve())
    projected: list[RecordingTake] = []
    resolved: list[RecordingTake] = []

    def resolve(
        proposed: RecordingTake, selection: RecordingSelection
    ) -> RecordingNavigationResult:
        assert rig.store._get_take(proposed.reservation.take_id).decision == "draft"
        assert projected == []
        resolved.append(proposed)
        return RecordingNavigationResult(
            catalog_sha256=selection.catalog_sha256,
            index_revision=999,
            next_prompt=proposed.reservation.group.catalog.prompts[1],
            complete=False,
        )

    rig.store.navigation_resolver = resolve
    rig.store.projection_hook = projected.append
    command = rig.command(take, advance=True)
    saved = rig.store.save(command)
    assert saved.operation.navigation.index_revision == 999
    assert projected == resolved
    assert reopen(rig).save(command) == saved
    assert len(resolved) == 1


def test_save_next_unknown_navigation_blocks_but_plain_save_and_trim_are_durable(rig: Rig) -> None:
    take = rig.seal(rig.reserve())
    with pytest.raises(ContractError, match="indexed"):
        rig.store.save(rig.command(take, advance=True))
    trim = RecordingFrameRange(source_frames=480, start_frame=10, end_frame=470)
    saved = rig.store.save(rig.command(take, frame_range=trim))
    assert saved.take.edit_revision == 1 and saved.take.frame_range == trim
    assert saved.take.analysis == "pending" and saved.take.export == "unprepared"
    assert not list((rig.layout.root / "derived").rglob("*.wav"))


@pytest.mark.parametrize(
    "change", ["catalog", "prompt", "decision", "edit", "source", "missing-take"]
)
def test_stale_save_fails_closed(rig: Rig, change: str) -> None:
    take = rig.seal(rig.reserve())
    command = rig.command(take).model_dump()
    if change in {"catalog", "prompt", "missing-take"}:
        key, value = {
            "catalog": ("catalog_sha256", "a" * 64),
            "prompt": ("prompt_id", "other"),
            "missing-take": ("take_id", None),
        }[change]
        command["selection"][key] = value
    elif change == "source":
        command["frame_range"]["source_frames"] = 481
    else:
        command[f"expected_{change}_revision"] = 1
    with pytest.raises(ContractError):
        rig.store.save(SaveTakeCommand.model_validate(command))
    assert rig.store.get_take(take.reservation.take_id).decision == "draft"


def test_operation_id_is_bound_across_commands_and_prompts(rig: Rig) -> None:
    reservation = rig.reserve()
    with pytest.raises(ContractError, match="already bound"):
        rig.reserve(prompt="synthetic-0001")
    receipt = rig.receipt(reservation)
    with pytest.raises(ContractError, match="already bound"):
        rig.store.register_seal(operation_id="reserve-1", receipt=receipt)
    take = rig.store.register_seal(operation_id="seal-1", receipt=receipt)
    assert rig.store.register_seal(operation_id="seal-1", receipt=receipt) == take
    with pytest.raises(ContractError, match="original operation"):
        rig.store.register_seal(operation_id="seal-other", receipt=receipt)
    saved = rig.store.save(rig.command(take))
    with pytest.raises(ContractError, match="already bound"):
        rig.store.save(rig.command(take, advance=True))
    with pytest.raises(ContractError, match="current draft"):
        rig.store.save(rig.command(saved.take, "second-save"))


@pytest.mark.parametrize(
    "mutation",
    [
        "size",
        "header",
        "public",
        "hardlink",
        "symlink",
        "partial",
        "receipt-device",
        "receipt-digest",
        "receipt-generation",
    ],
)
def test_register_rejects_incoherent_or_unsafe_source(rig: Rig, mutation: str) -> None:
    reservation = rig.reserve()
    receipt = rig.receipt(reservation)
    path = rig.layout.root / reservation.original_path
    if mutation == "size":
        path.write_bytes(path.read_bytes() + b"x")
    elif mutation == "header":
        with path.open("r+b") as stream:
            stream.write(b"FAIL")
    elif mutation == "public":
        path.chmod(0o644)
    elif mutation == "hardlink":
        os.link(path, path.with_name("linked.wav"))
    elif mutation == "symlink":
        target = path.with_name("renamed.wav")
        path.rename(target)
        path.symlink_to(target)
    elif mutation == "partial":
        (rig.layout.root / reservation.partial_path).touch(mode=0o600)
    else:
        payload = receipt.model_dump()
        if mutation == "receipt-device":
            payload["continuity"]["device_uid"] = "other"
        elif mutation == "receipt-digest":
            payload["reservation_sha256"] = "a" * 64
        else:
            payload["process_generation"] = "other"
        receipt = CaptureSealReceipt.model_validate(payload)
    with pytest.raises((ContractError, SecurityError, ValidationError)):
        rig.store.register_seal(operation_id="seal-1", receipt=receipt)
    assert rig.store.get_take(reservation.take_id).seal is None


def test_registration_reads_header_only_and_save_never_reads_audio(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    reservation = rig.reserve()
    receipt = rig.receipt(reservation)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("interactive path performed full audio verification")

    monkeypatch.setattr("cole_voice_lab.recording_takes.sha256_file", forbidden)
    take = rig.store.register_seal(operation_id="seal-1", receipt=receipt)
    assert rig.store.save(rig.command(take)).take.decision == "kept"
    assert rig.store.iter_takes()[0].decision == "kept"


def test_paging_is_bounded_and_validates_identity(rig: Rig) -> None:
    for index in range(3):
        rig.reserve(f"reserve-{index}")
    first = rig.store.iter_takes(limit=2)
    last = rig.store.iter_takes(after_take_id=first[-1].reservation.take_id, limit=2)
    assert len(first) == 2 and len(last) == 1
    assert rig.store.iter_takes(after_take_id=last[-1].reservation.take_id) == ()
    for limit in (0, 201):
        with pytest.raises(ValueError):
            rig.store.iter_takes(limit=limit)


def test_cancellation_commit_barrier_prevents_save_without_false_ack(rig: Rig) -> None:
    take = rig.seal(rig.reserve())
    token = RecordingCancellation()
    token.cancel()
    with pytest.raises(RecordingServiceError):
        rig.store.save(rig.command(take), commit_barrier=token.begin_commit)
    assert rig.store.get_take(take.reservation.take_id).decision == "draft"


def test_strict_operation_models_reject_conflicting_results(rig: Rig) -> None:
    take = rig.seal(rig.reserve())
    saved = rig.store.save(rig.command(take))
    path = rig.layout.root / "provenance/recording-takes/.operations/save-1.json"
    operation = RecordingTakeOperation.model_validate_json(path.read_bytes())
    for change in (
        {"operation_id": "wrong"},
        {"kind": "reserve"},
        {"save": None},
        {"committed_at": NOW - timedelta(days=1)},
        {"take": take},
    ):
        with pytest.raises(ValidationError):
            RecordingTakeOperation.model_validate(operation.model_dump() | change)
    assert operation.take == saved.take


def test_metadata_evidence_is_canonical_private_and_filename_bound(rig: Rig) -> None:
    reservation = rig.reserve()
    head = rig.layout.root / f"provenance/recording-takes/{reservation.take_id}/head.json"
    original = head.read_bytes()
    head.write_bytes(original + b" ")
    with pytest.raises(SecurityError, match="canonical"):
        rig.store.get_take(reservation.take_id)
    head.write_bytes(original)
    head.chmod(0o644)
    with pytest.raises(SecurityError, match="private"):
        rig.store.get_take(reservation.take_id)


def test_actual_backend_reserves_seals_and_saves_without_global_revision_staleness(
    rig: Rig,
) -> None:
    backend = RecordingTakesBackend(rig.store, rig.group_id)
    group = rig.store.get_group(rig.group_id)

    def request(data: dict[str, Any], take_id: str | None = None) -> ServiceRequest:
        return ServiceRequest.model_validate(
            {
                "data": data,
                "meta": {
                    "request_id": "request",
                    "process_generation": "service-generation",
                    "state_revision": 0,
                    "event_sequence": 0,
                    "take_id": take_id,
                },
            }
        )

    reserve = request(
        {
            "command": "reserve",
            "operation_id": "reserve-1",
            "group_id": rig.group_id,
            "recorder_generation": "helper-generation",
            "selection": {
                "catalog_sha256": group.catalog_sha256,
                "prompt_id": group.catalog.prompts[0].prompt_id,
                "take_id": None,
                "selection_revision": 0,
                "index_revision": 0,
            },
        }
    )
    backend.validate_request(reserve)
    event = backend.execute(reserve, RecordingCancellation())
    assert event.event == "reserved"
    reservation = event.take.reservation
    assert reservation.process_generation == "helper-generation"
    assert event.reservation_sha256 == canonical_sha256(reservation)
    receipt = rig.receipt(reservation)
    event = backend.execute(
        request(
            {"command": "register-seal", "operation_id": "seal-1", "receipt": receipt},
            reservation.take_id,
        ),
        RecordingCancellation(),
    )
    assert event.event == "take" and event.take.decision == "draft"
    saved = backend.execute(
        request(rig.command(event.take).model_dump(), reservation.take_id), RecordingCancellation()
    )
    assert saved.event == "saved"
    replay = backend.execute(reserve, RecordingCancellation())
    assert replay.take.seal is None
    assert rig.store.get_take(reservation.take_id).decision == "kept"


def test_restart_repairs_projection_even_after_store_head_was_written(rig: Rig) -> None:
    take = rig.seal(rig.reserve())
    command = rig.command(take)

    def fail(boundary: str) -> None:
        if boundary == "before-projection":
            raise OSError("synthetic crash before index commit")

    rig.store.fault_hook = fail
    with pytest.raises(OSError):
        rig.store.save(command)
    projected: list[RecordingTake] = []
    store = reopen(rig, projection_hook=projected.append)
    assert len(projected) == 1 and projected[0].decision == "kept"
    assert store.state_revision == 3
    assert len(projected) == 1
    assert not (rig.layout.root / "provenance/recording-takes/.pending.json").exists()
    assert store.save(command).take == projected[0]


def test_missing_store_head_after_commit_never_looks_empty(rig: Rig) -> None:
    rig.reserve()
    path = rig.layout.root / "provenance/recording-takes/.store-head.json"
    path.unlink()
    with pytest.raises(SecurityError, match="missing"):
        _ = reopen(rig).state_revision


def test_missing_source_prevents_save_without_audio_reads(rig: Rig) -> None:
    take = rig.seal(rig.reserve())
    (rig.layout.root / take.seal.original_path).unlink()
    with pytest.raises((OSError, SecurityError)):
        rig.store.save(rig.command(take))
    assert rig.store.get_take(take.reservation.take_id).decision == "draft"


@pytest.mark.parametrize(
    "mutation", ["none", "approval", "source-preserved-mtime", "replace-source"]
)
def test_prepared_corpus_gate_verifies_once_then_checks_exact_sources(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    from test_datasets import _corpus

    fixture = _corpus(tmp_path.resolve() / "corpus")
    store = RecordingTakeStore(fixture.layout, clock=lambda: NOW)
    catalog = fixture.suites["train-suite"]
    gate = store.prepare_corpus_gate(
        catalog, "cole", approval_path=fixture.gate_path, approval_sha256=fixture.gate_sha256
    )
    group = store.open_group(catalog, "cole", DEVICE, "host", prepared_gate=gate)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Record repeated expensive accepted-raw verification")

    monkeypatch.setattr("cole_voice_lab.recording_takes.load_pilot_gate", forbidden)
    if mutation == "approval":
        approval = fixture.layout.root / fixture.gate_path
        approval.write_bytes(approval.read_bytes() + b" ")
    elif mutation in {"source-preserved-mtime", "replace-source"}:
        source = next((fixture.layout.root / "raw").rglob("*.wav"))
        previous = source.stat()
        source.parent.chmod(0o700)
        source.chmod(0o600)
        if mutation == "replace-source":
            replacement = source.with_name("replacement.wav")
            replacement.write_bytes(source.read_bytes())
            replacement.chmod(0o400)
            replacement.replace(source)
        else:
            with source.open("r+b") as stream:
                stream.seek(44)
                stream.write(b"\x01\x02\x03")
            source.chmod(previous.st_mode & 0o777)
        os.utime(source, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    if mutation == "none":
        reservation = store.reserve(
            operation_id="authorized",
            group_id=group.group_id,
            prompt_id=catalog.prompts[0].prompt_id,
            process_generation="helper",
        )
        assert reservation.group.gate == gate
        assert store.open_group(catalog, "cole", DEVICE, "host", prepared_gate=gate) == group
    else:
        with pytest.raises(SecurityError, match="changed"):
            store.reserve(
                operation_id="denied",
                group_id=group.group_id,
                prompt_id=catalog.prompts[0].prompt_id,
                process_generation="helper",
            )


def test_gate_subject_catalog_and_time_are_verified(rig: Rig, tmp_path: Path) -> None:
    from test_datasets import _corpus

    fixture = _corpus(tmp_path.resolve() / "corpus")
    store = RecordingTakeStore(fixture.layout, clock=lambda: NOW)
    kwargs = dict(approval_path=fixture.gate_path, approval_sha256=fixture.gate_sha256)
    with pytest.raises(ContractError, match="only a corpus"):
        store.prepare_corpus_gate(synthetic_catalog(2), "cole", **kwargs)
    with pytest.raises(ContractError, match="another subject"):
        store.prepare_corpus_gate(fixture.suites["train-suite"], "different", **kwargs)
    store.clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(ContractError, match="future"):
        store.prepare_corpus_gate(fixture.suites["train-suite"], "cole", **kwargs)


def test_store_rejects_unsafe_metadata_locks_and_concurrent_writers(rig: Rig) -> None:
    lock = rig.layout.root / "provenance/recording-takes/.metadata.lock"
    with rig.store._metadata_lock():
        with pytest.raises(ContractError, match="another recording"):
            reopen(rig)
    lock.chmod(0o644)
    with pytest.raises(SecurityError, match="lock"):
        reopen(rig)


def test_normal_revision_reads_do_not_rewrite_or_fsync(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.reserve()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("completed metadata read attempted a redundant fsync")

    monkeypatch.setattr(os, "fsync", forbidden)
    assert rig.store.state_revision == 1
    assert rig.store.recover() == 1


@pytest.mark.parametrize(
    "kind",
    [
        "invalid-json",
        "oversized",
        "wrong-group-id",
        "wrong-take-id",
        "reservation-change",
        "head-conflict",
    ],
)
def test_corrupt_evidence_fails_closed(rig: Rig, kind: str) -> None:
    reservation = rig.reserve()
    head = rig.layout.root / f"provenance/recording-takes/{reservation.take_id}/head.json"
    if kind == "invalid-json":
        head.write_bytes(b"{}")
    elif kind == "oversized":
        head.write_bytes(b" " * (1_048_576 + 1))
    elif kind == "wrong-group-id":
        group = rig.store.get_group(rig.group_id)
        write_canonical_json(
            rig.layout.root / f"provenance/recording-groups/{rig.group_id}.json",
            group.model_copy(update={"group_id": "other"}),
        )
        with pytest.raises(SecurityError, match="filename"):
            rig.store.get_group(rig.group_id)
        return
    elif kind == "wrong-take-id":
        copied = rig.layout.root / "provenance/recording-takes/take-other"
        copied.mkdir(mode=0o700)
        write_canonical_json(copied / "head.json", rig.store.get_take(reservation.take_id))
        with pytest.raises(SecurityError, match="filename"):
            rig.store.get_take("take-other")
        return
    elif kind == "reservation-change":
        changed = reservation.model_copy(update={"maximum_frames": 1000})
        write_canonical_json(head.parent / "reservation.json", changed)
    elif kind == "head-conflict":
        current = rig.store.get_take(reservation.take_id)
        write_canonical_json(head, current.model_copy(update={"analysis": "running"}))
        with pytest.raises(SecurityError, match="conflicting"):
            rig.reserve()
        return
    with pytest.raises((SecurityError, ContractError)):
        rig.store.get_take(reservation.take_id)


def test_backend_reports_unknown_history_and_named_failures(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = RecordingTakesBackend(rig.store, rig.group_id)
    group = rig.store.get_group(rig.group_id)

    def request(data: dict[str, Any], take_id: str | None = None) -> ServiceRequest:
        return ServiceRequest.model_validate(
            {
                "data": data,
                "meta": {
                    "request_id": "request",
                    "process_generation": "service-generation",
                    "state_revision": 0,
                    "event_sequence": 0,
                    "take_id": take_id,
                },
            }
        )

    snapshot = backend.execute(request({"command": "snapshot"}), RecordingCancellation())
    assert snapshot.completeness == "unknown" and snapshot.has_more is False
    history = request({"command": "history", "catalog_sha256": group.catalog_sha256})
    assert backend.execute(history, RecordingCancellation()) == snapshot
    for data in (
        {"command": "history", "catalog_sha256": "0" * 64},
        {"command": "history", "catalog_sha256": group.catalog_sha256, "cursor": "unprepared"},
    ):
        req = request(data, "unknown" if data["command"] == "recover" else None)
        with pytest.raises(RecordingServiceError):
            backend.execute(req, RecordingCancellation())
    recovered = backend.execute(
        request({"command": "recover", "operation_id": "recover", "take_id": "unknown"}, "unknown"),
        RecordingCancellation(),
    )
    assert recovered.event == "recovery" and recovered.page.entries[0].status == "unavailable"
    assert recovered.page.entries[0].error is not None
    reserve = request(
        {
            "command": "reserve",
            "operation_id": "reserve-1",
            "group_id": "other",
            "recorder_generation": "helper",
            "selection": {
                "catalog_sha256": group.catalog_sha256,
                "prompt_id": group.catalog.prompts[0].prompt_id,
                "take_id": None,
                "selection_revision": 0,
                "index_revision": 0,
            },
        }
    )
    with pytest.raises(RecordingServiceError, match="context"):
        backend.validate_request(reserve)
    take = rig.seal(rig.reserve())
    saved = request(rig.command(take).model_dump(), take.reservation.take_id)
    for error, code in (
        (SecurityError("synthetic"), "RECORDING.SOURCE_CHANGED"),
        (ContractError("synthetic"), "RECORDING.INVALID_TRANSITION"),
        (OSError("synthetic"), "RECORDING.WRITE_FAILED"),
    ):

        def fail(*args: Any, failure: Exception = error, **kwargs: Any) -> Any:
            raise failure

        monkeypatch.setattr(rig.store, "save", fail)
        with pytest.raises(RecordingServiceError) as caught:
            backend.execute(saved, RecordingCancellation())
        assert caught.value.recording_error.code == code


def test_cli_forwards_durable_group_identity(monkeypatch: pytest.MonkeyPatch, rig: Rig) -> None:
    from cole_voice_lab.cli import main

    calls: list[list[str]] = []
    monkeypatch.setattr(
        "cole_voice_lab.recording_service.main", lambda arguments: calls.append(arguments) or 0
    )
    assert (
        main(
            [
                "recording",
                "service",
                "--asset-root",
                str(rig.layout.root),
                "--catalog-sha256",
                "a" * 64,
                "--recording-group-id",
                rig.group_id,
                "--process-generation",
                "service-gen",
                "--build-identity",
                "build.json",
                "--expected-client-build",
                "client.json",
            ]
        )
        == 0
    )
    index = calls[0].index("--recording-group-id")
    assert calls[0][index + 1] == rig.group_id


def test_projection_reads_do_not_take_foreground_metadata_lock(rig: Rig) -> None:
    take = rig.seal(rig.reserve())
    reader = reopen(rig)
    with rig.store._metadata_lock():
        assert reader.get_take_for_projection(take.reservation.take_id) == take
        assert reader.iter_takes_for_projection() == (take,)


@pytest.mark.parametrize("during_read", [False, True])
def test_projection_reads_defer_an_incomplete_commit(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, during_read: bool
) -> None:
    take = rig.seal(rig.reserve())
    pending = rig.layout.root / "provenance/recording-takes/.pending.json"
    reader = reopen(rig)
    original_read = reader._get_take

    def mark_pending(take_id: str) -> RecordingTake:
        result = original_read(take_id)
        pending.write_bytes(b"uncommitted synthetic marker")
        pending.chmod(0o600)
        return result

    if during_read:
        monkeypatch.setattr(reader, "_get_take", mark_pending)
    else:
        pending.write_bytes(b"uncommitted synthetic marker")
        pending.chmod(0o600)
    with pytest.raises(RecordingMetadataBusyError):
        reader.get_take_for_projection(take.reservation.take_id)
    with pytest.raises(RecordingMetadataBusyError):
        reader.iter_takes_for_projection()


def test_slow_background_projection_read_does_not_block_actual_save(rig: Rig) -> None:
    import concurrent.futures
    import threading

    take = rig.seal(rig.reserve())
    reader = reopen(rig)
    reading, release = threading.Event(), threading.Event()
    original_read = reader._get_take

    def delayed_read(take_id: str) -> RecordingTake:
        reading.set()
        assert release.wait(5)
        return original_read(take_id)

    reader._get_take = delayed_read  # type: ignore[method-assign]
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(reader.iter_takes_for_projection)
        try:
            assert reading.wait(5)
            saved = rig.store.save(rig.command(take))
            assert saved.take.decision == "kept"
        finally:
            release.set()
        assert future.result(timeout=5) == (saved.take,)
