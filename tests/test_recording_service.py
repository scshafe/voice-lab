from __future__ import annotations

import json
import os
import select
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_recording_contracts import synthetic_contracts

from cole_voice_lab.canonical import canonical_json_bytes, canonical_sha256, write_canonical_json
from cole_voice_lab.corpus_store import open_corpus_store
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.models import PromptSuite
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    MAX_CONTROL_FRAME_BYTES,
    MAX_TELEMETRY_FRAME_BYTES,
    AcknowledgedEvent,
    CancelCommand,
    CatalogCommand,
    CatalogEvent,
    CatalogReference,
    CatalogSnapshot,
    HandshakeCommand,
    HistoryCommand,
    OpenContextCommand,
    RecordingBuildIdentity,
    RecordingHistoryPage,
    RecordingMessageMeta,
    RecordingResponse,
    ReliableEvent,
    ServiceRequest,
    ShutdownCommand,
    SnapshotCommand,
    recording_frame_bytes,
)
from cole_voice_lab.recording_ownership import (
    CAPTURE_OWNERSHIP_RELATIVE_PATH,
    acquire_capture_ownership,
    capture_ownership,
    validate_capture_ownership,
)
from cole_voice_lab.recording_service import (
    MAX_PENDING_REQUESTS,
    CoalescingRecordingTelemetry,
    RecordingCancellation,
    RecordingFrameDecoder,
    RecordingResponseTracker,
    RecordingServiceError,
    RecordingServiceSession,
    RecordingWorkspaceBackend,
    UnpreparedRecordingBackend,
    decode_service_request,
    recording_service_build_identity,
    serve_recording_pipes,
)


def build_identity() -> RecordingBuildIdentity:
    return synthetic_contracts()["capture-seal"].recorder_build


def request(
    data: Any,
    *,
    request_id: str = "request-1",
    revision: int = 0,
    generation: str = "generation-1",
    sequence: int = 0,
    take_id: str | None = None,
) -> ServiceRequest:
    return ServiceRequest(
        data=data,
        meta=RecordingMessageMeta(
            request_id=request_id,
            process_generation=generation,
            state_revision=revision,
            event_sequence=sequence,
            take_id=take_id,
        ),
    )


def handshake(
    *, client: RecordingBuildIdentity | None = None, service: RecordingBuildIdentity | None = None
) -> ServiceRequest:
    return request(
        HandshakeCommand(
            role="ui",
            build=client or build_identity(),
            expected_peer_build=service or build_identity(),
        ),
        request_id="handshake",
    )


@pytest.fixture
def session() -> Iterator[RecordingServiceSession]:
    session = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=UnpreparedRecordingBackend("a" * 64),
    )
    try:
        yield session
    finally:
        session.close()


def prepared(session: RecordingServiceSession) -> None:
    response = session.submit(handshake())
    assert response is not None and response.error is None
    list(session.completed())


def await_responses(
    session: RecordingServiceSession, count: int = 1, *, timeout: float = 2
) -> list[RecordingResponse]:
    deadline = time.monotonic() + timeout
    responses = []
    while len(responses) < count and time.monotonic() < deadline:
        responses.extend(session.completed())
        if len(responses) < count:
            threading.Event().wait(0.002)
    assert len(responses) == count
    return responses


@pytest.mark.parametrize("split", list(range(1, 15)))
def test_incremental_frames_split_at_every_prefix_and_payload_boundary(split: int) -> None:
    decoder = RecordingFrameDecoder()
    frames = b"\x00\x00\x00\x03abc\x00\x00\x00\x04defg"
    result = [*decoder.feed(frames[:split]), *decoder.feed(frames[split:])]
    decoder.finish()
    assert result == [b"abc", b"defg"]
    assert decoder.buffered_bytes == 0


@pytest.mark.parametrize("payload", [b"\x00", b"\x00\x00\x00\x03", b"\x00\x00\x00\x03a"])
def test_truncated_prefix_or_body_fails_closed(payload: bytes) -> None:
    decoder = RecordingFrameDecoder()
    list(decoder.feed(payload))
    with pytest.raises(RecordingServiceError, match="incomplete"):
        decoder.finish()


@pytest.mark.parametrize("length", [0, MAX_CONTROL_FRAME_BYTES + 1, 0xFFFFFFFF])
def test_invalid_frame_length_is_rejected_before_body_allocation(length: int) -> None:
    decoder = RecordingFrameDecoder()
    with pytest.raises(RecordingServiceError, match="bounds"):
        list(decoder.feed(length.to_bytes(4, "big")))
    assert decoder.buffered_bytes == 0
    with pytest.raises(RecordingServiceError, match="already failed"):
        list(decoder.feed(b"safe"))


def test_telemetry_frame_limit_is_independent() -> None:
    decoder = RecordingFrameDecoder(telemetry=True)
    with pytest.raises(RecordingServiceError):
        list(decoder.feed((MAX_TELEMETRY_FRAME_BYTES + 1).to_bytes(4, "big")))


@pytest.mark.parametrize(
    "payload",
    [
        b"[]",
        b"{",
        b"\xff",
        b'{"value":NaN}',
        b'{"data":{},"data":{}}',
        b"[" * 1100,
        b"x" * (MAX_CONTROL_FRAME_BYTES + 1),
    ],
)
def test_json_rejects_invalid_utf8_duplicate_keys_nonfinite_and_depth(payload: bytes) -> None:
    with pytest.raises(RecordingServiceError):
        decode_service_request(payload)


def test_version_mismatch_has_stable_error_code() -> None:
    payload = handshake().model_dump(mode="json")
    payload["meta"]["protocol_version"] = "future-version"
    with pytest.raises(RecordingServiceError) as failure:
        decode_service_request(canonical_json_bytes(payload))
    assert failure.value.recording_error.code == "RECORDING.VERSION_MISMATCH"


def test_valid_request_roundtrips_strict_decoder() -> None:
    assert decode_service_request(canonical_json_bytes(handshake())) == handshake()


def test_handshake_exact_build_and_generation_before_metadata(
    session: RecordingServiceSession,
) -> None:
    before = session.submit(request(SnapshotCommand()))
    assert before is not None and before.error is not None
    assert before.error.code == "RECORDING.VERSION_MISMATCH"
    wrong = build_identity().model_copy(update={"source_sha256": "f" * 64})
    failed = session.submit(handshake(client=wrong))
    assert failed is not None and failed.error is not None
    assert failed.error.code == "RECORDING.BUILD_MISMATCH"
    good = session.submit(handshake())
    assert good is not None and good.error is None


def test_replay_returns_exact_response_without_reexecuting_or_overwriting(
    session: RecordingServiceSession,
) -> None:
    original = session.submit(handshake())
    assert session.submit(handshake()) == original
    mismatch = session.submit(request(SnapshotCommand(), request_id="handshake"))
    assert mismatch is not None and mismatch.error is not None
    assert session.submit(handshake()) == original


def test_stale_generation_and_future_sequence_are_explicit(
    session: RecordingServiceSession,
) -> None:
    prepared(session)
    stale = session.submit(request(SnapshotCommand(), generation="old-generation"))
    assert stale is not None and stale.error is not None
    assert stale.error.code == "RECORDING.STALE_GENERATION"
    future = session.submit(request(SnapshotCommand(), sequence=10_000))
    assert future is not None and future.error is not None
    assert future.error.code == "RECORDING.INVALID_MESSAGE"


def test_unknown_history_and_unprepared_seal_never_claim_saved(
    session: RecordingServiceSession,
) -> None:
    prepared(session)
    session.submit(request(SnapshotCommand()))
    snapshot = await_responses(session)[0]
    assert isinstance(snapshot.data, RecordingHistoryPage)
    assert snapshot.data.completeness == "unknown"
    values = synthetic_contracts()
    seal_request = ServiceRequest.model_validate(
        {
            "data": {
                "command": "register-seal",
                "operation_id": "op-seal",
                "receipt": values["capture-seal"],
            },
            "meta": request(SnapshotCommand(), request_id="seal").meta.model_copy(
                update={"take_id": "synthetic-take"}
            ),
        }
    )
    session.submit(seal_request)
    response = await_responses(session)[0]
    assert response.error is not None and response.error.code == "RECORDING.GATE_NOT_READY"


@pytest.mark.parametrize(
    "data,revision,expected",
    [
        (SnapshotCommand(), 1, "RECORDING.STALE_REVISION"),
        (HistoryCommand(catalog_sha256="b" * 64), 0, "RECORDING.HISTORY_UNKNOWN"),
        (
            HistoryCommand(catalog_sha256="a" * 64, cursor="old-cursor"),
            0,
            "RECORDING.HISTORY_UNKNOWN",
        ),
    ],
)
def test_backend_guards_revision_catalog_and_cursor(
    session: RecordingServiceSession, data: Any, revision: int, expected: str
) -> None:
    prepared(session)
    session.submit(request(data, revision=revision))
    response = await_responses(session)[0]
    assert response.error is not None and response.error.code == expected


class BlockingMetadataBackend(UnpreparedRecordingBackend):
    def __init__(self) -> None:
        super().__init__("a" * 64)
        self.entered = threading.Event()
        self.released = threading.Event()
        self.calls = 0

    def execute(
        self, request: ServiceRequest, cancellation: RecordingCancellation
    ) -> ReliableEvent:
        self.calls += 1
        self.entered.set()
        while not self.released.is_set():
            cancellation.wait(0.01)
            cancellation.check()
        return AcknowledgedEvent(operation_id="op-recovery", action="recovered")


def blocking_session(backend: BlockingMetadataBackend) -> RecordingServiceSession:
    session = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=backend,
    )
    prepared(session)
    return session


def recovery_request(request_id: str = "recovery") -> ServiceRequest:
    return ServiceRequest.model_validate(
        {
            "data": {"command": "recover", "operation_id": "op-recovery"},
            "meta": request(SnapshotCommand(), request_id=request_id).meta,
        }
    )


def test_duplicate_pending_operation_coalesces_and_cancel_bypasses_metadata() -> None:
    backend = BlockingMetadataBackend()
    session = blocking_session(backend)
    try:
        assert session.submit(recovery_request()) is None
        assert backend.entered.wait(1)
        assert session.submit(recovery_request()) is None
        started = time.monotonic()
        cancellation = session.submit(
            request(
                CancelCommand(
                    operation_id="op-cancel",
                    cancelled_operation_id="op-recovery",
                ),
                request_id="cancel",
            )
        )
        assert time.monotonic() - started < 0.1
        assert cancellation is not None and cancellation.error is None
        responses = await_responses(session, 2)
        assert any(
            response.error and response.error.code == "RECORDING.CANCELLED"
            for response in responses
        )
        assert backend.calls == 1
    finally:
        session.close()


def test_metadata_admission_is_bounded_and_parent_loss_cancels_pending_work() -> None:
    backend = BlockingMetadataBackend()
    session = blocking_session(backend)
    try:
        for index in range(MAX_PENDING_REQUESTS):
            session.submit(recovery_request(f"request-{index}"))
        assert backend.entered.wait(1)
        overflow = session.submit(recovery_request("overflow"))
        assert overflow is not None and overflow.error is not None and overflow.error.retryable
        assert session.pending_count == MAX_PENDING_REQUESTS
    finally:
        session.close()
    assert session.pending_count == 0


def test_commit_cannot_be_relabelled_as_cancelled() -> None:
    token = RecordingCancellation()
    token.begin_commit()
    assert not token.cancel()
    token.check()
    earlier = RecordingCancellation()
    assert earlier.cancel()
    with pytest.raises(RecordingServiceError):
        earlier.begin_commit()


def test_telemetry_coalescing_does_not_block_reliable_control(
    session: RecordingServiceSession,
) -> None:
    telemetry = synthetic_contracts()["recording-telemetry"]
    slot = CoalescingRecordingTelemetry(
        take_id=telemetry.data.take_id, process_generation=telemetry.data.process_generation
    )
    for _ in range(1000):
        slot.publish(telemetry)
    prepared(session)
    assert slot.take() == telemetry
    assert slot.take() is None


def test_response_tracker_detects_gap_and_requires_explicit_snapshot(
    session: RecordingServiceSession,
) -> None:
    response = session.submit(handshake())
    assert response is not None
    tracker = RecordingResponseTracker("generation-1")
    assert tracker.accept(response)
    assert not tracker.accept(response)
    gap = response.model_copy(
        update={"meta": response.meta.model_copy(update={"event_sequence": 4})}
    )
    with pytest.raises(RecordingServiceError, match="gap"):
        tracker.accept(gap)
    assert tracker.needs_snapshot
    with pytest.raises(RecordingServiceError, match="snapshot"):
        tracker.accept(gap, snapshot=True)
    snapshot = gap.model_copy(
        update={
            "data": RecordingHistoryPage(
                catalog_sha256="a" * 64,
                index_revision=0,
                completeness="unknown",
                has_more=False,
            )
        }
    )
    assert tracker.accept(snapshot, snapshot=True)
    assert not tracker.needs_snapshot


def test_shared_capture_ownership_conflicts_without_mutating_lock(layout: AssetLayout) -> None:
    with capture_ownership(layout) as descriptor:
        path = layout.root / CAPTURE_OWNERSHIP_RELATIVE_PATH
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert not os.get_inheritable(descriptor)
        with pytest.raises(ContractError, match="already open"):
            acquire_capture_ownership(layout)
        duplicate = os.dup(descriptor)
        try:
            validate_capture_ownership(layout, duplicate)
        finally:
            os.close(duplicate)
    with capture_ownership(layout):
        assert path.exists()


@pytest.fixture
def layout(tmp_path: Path) -> AssetLayout:
    return AssetLayout.initialize(tmp_path.resolve() / "assets")


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "replaced"])
def test_shared_capture_ownership_rejects_unsafe_lock_identity(
    layout: AssetLayout, kind: str
) -> None:
    descriptor = acquire_capture_ownership(layout)
    path = layout.root / CAPTURE_OWNERSHIP_RELATIVE_PATH
    try:
        if kind == "public":
            path.chmod(0o644)
        elif kind == "hardlink":
            os.link(path, path.with_name("alias"))
        else:
            path.rename(path.with_name("prior"))
            if kind == "symlink":
                path.symlink_to(path.with_name("prior"))
            else:
                path.touch(mode=0o600)
        with pytest.raises((SecurityError, OSError)):
            validate_capture_ownership(layout, descriptor)
        if kind in {"hardlink", "public", "symlink"}:
            with pytest.raises((SecurityError, OSError)):
                acquire_capture_ownership(layout)
    finally:
        os.close(descriptor)


class ServiceProcessEvidence:
    """Retain private subprocess failures without changing the service or its deadlines."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="service-evidence-", dir=directory))
        self.stderr = os.fdopen(
            os.open(self.directory / "stderr.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
            "wb",
        )
        self.last_wire = "no wire bytes observed"

    def wire(self, direction: str, payload: bytes) -> None:
        assert direction in {"sent", "received"}
        with os.fdopen(
            os.open(
                self.directory / f"{direction}.frames",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND,
                0o600,
            ),
            "ab",
        ) as stream:
            stream.write(payload)
        self.last_wire = f"{direction}: {payload[-1024:]!r}"

    def describe(self, returncode: int | None = None) -> str:
        if not self.stderr.closed:
            self.stderr.flush()
        with (self.directory / "stderr.log").open("rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 8192))
            stderr = stream.read().decode("utf-8", errors="replace")
        return (
            f"service evidence: {self.directory}; returncode={returncode}; "
            f"stderr tail={stderr!r}; last wire={self.last_wire}"
        )

    def exit(self, returncode: int | None) -> None:
        with os.fdopen(
            os.open(self.directory / "exit.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
            "w",
        ) as stream:
            json.dump(
                {"returncode": returncode, "observed_monotonic_ns": time.monotonic_ns()}, stream
            )
            stream.write("\n")


def read_response(
    descriptor: int, *, timeout: float = 2.0, evidence: ServiceProcessEvidence | None = None
) -> RecordingResponse:
    decoder = RecordingFrameDecoder()
    deadline = time.monotonic() + timeout
    received = bytearray()
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([descriptor], [], [], deadline - time.monotonic())
            if readable:
                data = os.read(descriptor, 1)
                assert data, "service exited before response"
                received.extend(data)
                for payload in decoder.feed(data):
                    return RecordingResponse.model_validate_json(payload)
        raise AssertionError("service response timed out")
    finally:
        if evidence is not None:
            evidence.wire("received", bytes(received))


def test_real_process_handshake_snapshot_unprepared_registration_and_clean_shutdown(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    layout = AssetLayout.initialize(root / "assets")
    build = recording_service_build_identity()
    own_file, peer_file = root / "service-build.json", root / "client-build.json"
    write_canonical_json(own_file, build)
    write_canonical_json(peer_file, build_identity())
    evidence = ServiceProcessEvidence(root)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cole_voice_lab.recording_service",
            "--asset-root",
            str(layout.root),
            "--catalog-sha256",
            "a" * 64,
            "--process-generation",
            "generation-1",
            "--build-identity",
            str(own_file),
            "--expected-client-build",
            str(peer_file),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=evidence.stderr,
    )
    assert process.stdin is not None and process.stdout is not None

    def send(message: ServiceRequest) -> None:
        frame = recording_frame_bytes(message)
        evidence.wire("sent", frame)
        assert process.stdin is not None
        process.stdin.write(frame)
        process.stdin.flush()

    try:
        send(handshake(service=build))
        hello = read_response(process.stdout.fileno(), evidence=evidence)
        assert hello.error is None
        send(request(SnapshotCommand()))
        snapshot = read_response(process.stdout.fileno(), evidence=evidence)
        assert (
            isinstance(snapshot.data, RecordingHistoryPage)
            and snapshot.data.completeness == "unknown"
        )
        receipt_request = ServiceRequest.model_validate(
            {
                "data": {
                    "command": "register-seal",
                    "operation_id": "op-real-seal",
                    "receipt": synthetic_contracts()["capture-seal"],
                },
                "meta": request(SnapshotCommand(), request_id="real-seal").meta.model_copy(
                    update={"take_id": "synthetic-take"}
                ),
            }
        )
        send(receipt_request)
        registration = read_response(process.stdout.fileno(), evidence=evidence)
        assert registration.error is not None
        assert registration.error.code == "RECORDING.GATE_NOT_READY"
        send(request(ShutdownCommand(), request_id="shutdown"))
        shutdown = read_response(process.stdout.fileno(), evidence=evidence)
        assert isinstance(shutdown.data, AcknowledgedEvent) and shutdown.data.action == "shutdown"
        assert process.wait(timeout=3) == 0, evidence.describe(process.returncode)
    except BaseException:
        print(evidence.describe(process.poll()))
        raise
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        evidence.exit(process.returncode)
        evidence.stderr.close()


def test_pipe_parent_eof_cancels_metadata_without_waiting_for_qc() -> None:
    backend = BlockingMetadataBackend()
    session = blocking_session(backend)
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    failures: list[BaseException] = []

    def run() -> None:
        try:
            serve_recording_pipes(session, input_read, output_write)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        os.write(input_write, recording_frame_bytes(recovery_request()))
        assert backend.entered.wait(1)
        started = time.monotonic()
        os.close(input_write)
        thread.join(timeout=1.5)
        assert not thread.is_alive() and time.monotonic() - started < 1.5
        assert failures == [] and session.closed
    finally:
        for descriptor in (input_read, output_read, output_write):
            os.close(descriptor)


def test_stalled_output_pipe_has_bounded_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cole_voice_lab.recording_service.CONTROL_OUTPUT_DEADLINE_SECONDS", 0.05)
    service = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=UnpreparedRecordingBackend("a" * 64),
    )
    input_read, input_write = os.pipe()
    output_read, output_write = os.pipe()
    os.set_blocking(output_write, False)
    try:
        while True:
            os.write(output_write, b"x" * 4096)
    except BlockingIOError:
        pass
    failures: list[BaseException] = []

    def run() -> None:
        try:
            serve_recording_pipes(service, input_read, output_write)
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        os.write(input_write, recording_frame_bytes(handshake()))
        thread.join(timeout=1)
        assert not thread.is_alive()
        assert len(failures) == 1 and isinstance(failures[0], RecordingServiceError)
        assert failures[0].recording_error.code == "RECORDING.PARENT_LOST"
    finally:
        for descriptor in (input_read, input_write, output_read, output_write):
            os.close(descriptor)


def test_service_rejects_regular_files_as_control_transport(
    session: RecordingServiceSession, tmp_path: Path
) -> None:
    with (tmp_path / "not-a-pipe").open("w+b") as stream:
        with pytest.raises(RecordingServiceError, match="inherited local pipes"):
            serve_recording_pipes(session, stream.fileno(), stream.fileno())


def test_shutdown_remains_reachable_during_metadata_and_prevents_more_work() -> None:
    backend = BlockingMetadataBackend()
    service = blocking_session(backend)
    service.submit(recovery_request())
    assert backend.entered.wait(1)
    response = service.submit(request(ShutdownCommand(), request_id="shutdown"))
    assert response is not None and isinstance(response.data, AcknowledgedEvent)
    assert service.closed
    closed = service.submit(request(SnapshotCommand(), request_id="closed"))
    assert closed is not None and closed.error is not None
    assert closed.error.code == "RECORDING.PARENT_LOST"
    service.close()


def test_cancel_missing_operation_requires_reconciliation(session: RecordingServiceSession) -> None:
    prepared(session)
    cancelled = session.submit(
        request(
            CancelCommand(
                operation_id="op-cancel",
                cancelled_operation_id="already-committed",
            )
        )
    )
    assert cancelled is not None and cancelled.error is not None
    assert cancelled.error.code == "RECORDING.INVALID_TRANSITION"


def test_pending_request_id_cannot_be_reused_for_different_operation() -> None:
    backend = BlockingMetadataBackend()
    service = blocking_session(backend)
    try:
        service.submit(recovery_request())
        assert backend.entered.wait(1)
        collision = service.submit(request(SnapshotCommand(), request_id="recovery"))
        assert collision is not None and collision.error is not None
        assert collision.error.code == "RECORDING.INVALID_MESSAGE"
    finally:
        service.close()


def test_unexpected_backend_error_is_named_without_exposing_input(
    session: RecordingServiceSession,
) -> None:
    class FailedBackend(UnpreparedRecordingBackend):
        def execute(
            self, request: ServiceRequest, cancellation: RecordingCancellation
        ) -> ReliableEvent:
            raise OSError("Synthetic private transcript must not reach the wire")

    session.backend = FailedBackend("a" * 64)
    prepared(session)
    session.submit(request(SnapshotCommand()))
    response = await_responses(session)[0]
    assert response.error is not None and response.error.code == "RECORDING.WRITE_FAILED"
    assert b"transcript" not in canonical_json_bytes(response)


def test_metadata_responses_remain_sequenced_with_concurrent_control() -> None:
    backend = BlockingMetadataBackend()
    service = blocking_session(backend)
    try:
        service.submit(recovery_request())
        assert backend.entered.wait(1)
        backend.released.set()
        service.submit(
            request(
                HandshakeCommand(
                    role="ui",
                    build=build_identity(),
                    expected_peer_build=build_identity(),
                ),
                request_id="second-handshake",
            )
        )
        responses = await_responses(service, 2)
        assert [response.meta.event_sequence for response in responses] == [2, 3]
    finally:
        service.close()


def test_replay_storage_is_bounded_without_losing_fresh_request_semantics(
    session: RecordingServiceSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("cole_voice_lab.recording_service.MAX_REPLAY_RESPONSES", 2)
    prepared(session)
    for index in range(4):
        session.submit(request(SnapshotCommand(), request_id=f"snapshot-{index}"))
        await_responses(session)
    assert len(session._replay) == 2


def test_telemetry_ignores_old_generation_and_frames_even_after_consumption() -> None:
    telemetry = synthetic_contracts()["recording-telemetry"]
    slot = CoalescingRecordingTelemetry(
        take_id=telemetry.data.take_id, process_generation=telemetry.data.process_generation
    )
    assert slot.publish(telemetry)
    assert slot.take() == telemetry
    assert not slot.publish(telemetry)
    wrong_generation = telemetry.model_copy(
        update={
            "meta": telemetry.meta.model_copy(update={"process_generation": "old-generation"}),
            "data": telemetry.data.model_copy(update={"process_generation": "old-generation"}),
        }
    )
    assert not slot.publish(wrong_generation)


def test_shared_capture_lock_survives_parent_descriptor_close_in_inherited_child(
    layout: AssetLayout,
) -> None:
    descriptor = acquire_capture_ownership(layout)
    program = "\n".join(
        [
            "import fcntl, os, sys",
            "from pathlib import Path",
            "from cole_voice_lab.paths import AssetLayout",
            "from cole_voice_lab.recording_ownership import validate_capture_ownership",
            "descriptor = int(sys.argv[2])",
            "validate_capture_ownership(AssetLayout.validate(Path(sys.argv[1])), descriptor)",
            "fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)",
            "sys.stdout.buffer.write(b'ready\\n'); sys.stdout.buffer.flush()",
            "sys.stdin.buffer.read(1)",
        ]
    )
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(layout.root), str(descriptor)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        pass_fds=(descriptor,),
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        assert select.select([process.stdout], [], [], 2)[0]
        assert process.stdout.readline() == b"ready\n"
        os.close(descriptor)
        descriptor = -1
        with pytest.raises(ContractError, match="already open"):
            acquire_capture_ownership(layout)
        process.stdin.write(b"q")
        process.stdin.flush()
        assert process.wait(timeout=2) == 0
        with capture_ownership(layout):
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


SUBJECT = "synthetic-subject"


def activate_catalog(
    layout: AssetLayout,
    suite: PromptSuite,
    *,
    catalog_id: str = "synthetic-corpus",
    suite_id: str | None = None,
    subject_id: str = SUBJECT,
) -> tuple[CatalogReference, CatalogSnapshot]:
    """Publish ``suite`` into the root's corpus store and make it the subject's catalog.

    The suite's prompts must be new to the store; the catalog takes ``suite_id`` (default:
    ``catalog_id``) and the suite's purpose.
    """
    from test_recording_takes import publish_catalog

    reference, snapshot = publish_catalog(layout, suite, catalog_id=catalog_id, suite_id=suite_id)
    with open_corpus_store(layout, create=False) as corpus:
        corpus.activate(subject_id, reference)
    return reference, snapshot


def workspace_backend(layout: AssetLayout) -> RecordingWorkspaceBackend:
    return RecordingWorkspaceBackend(layout, SUBJECT, discover=False)


def execute(backend: RecordingWorkspaceBackend, data: Any) -> ReliableEvent:
    backend.validate_request(request(data, revision=backend.state_revision))
    return backend.execute(request(data, revision=backend.state_revision), RecordingCancellation())


def refused(backend: RecordingWorkspaceBackend, data: Any, code: str, match: str) -> None:
    with pytest.raises(RecordingServiceError, match=match) as failure:
        execute(backend, data)
    assert failure.value.recording_error.code == code


def referenced_context(reference: CatalogReference, **changes: Any) -> OpenContextCommand:
    from test_recording_takes import DEVICE

    return OpenContextCommand.model_validate(
        {
            "operation_id": "context-referenced",
            "subject_id": SUBJECT,
            "catalog_reference": reference,
            "capture_device": DEVICE,
            "capture_host": "host",
        }
        | changes
    )


def lease_is_free(layout: AssetLayout) -> bool:
    """Whether no process holds the corpus store open (its shared lease is released)."""
    import fcntl

    descriptor = os.open(layout.root / "provenance/corpus/.corpus.lease", os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(descriptor)
    return True


def test_catalog_request_serves_the_active_catalog_pinned_for_the_process(
    tmp_path: Path,
) -> None:
    from test_recording_takes import store_suite

    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    backend = workspace_backend(layout)
    try:
        catalog = CatalogCommand(operation_id="op-catalog")
        for data in (catalog, SnapshotCommand()):
            refused(backend, data, "CORPUS.STORE_MISSING", "voice-lab catalog init")
        # Asking never creates a store.
        assert not (layout.root / "provenance/corpus").exists()
        with open_corpus_store(layout, create=True):
            pass
        for data in (catalog, SnapshotCommand()):
            refused(backend, data, "CORPUS.NO_ACTIVE_CATALOG", "voice-lab catalog activate")
        reference, snapshot = activate_catalog(layout, store_suite(3))
        # Failures pinned nothing: the next request resolves the catalog just activated.
        event = execute(backend, catalog)
        assert event == CatalogEvent(
            operation_id="op-catalog", catalog=snapshot.suite, reference=reference
        )
        page = execute(backend, SnapshotCommand())
        assert isinstance(page, RecordingHistoryPage)
        assert page.catalog_sha256 == reference.catalog_sha256
        # The service read the store and closed it again; the CLI can edit it meanwhile.
        assert lease_is_free(layout)
        with open_corpus_store(layout, create=False) as corpus:
            corpus.edit_prompt("spoken-0001", text="An edited synthetic sentence.")
            newer = corpus.publish_version("synthetic-corpus")
            corpus.activate(SUBJECT, newer)
        # Pinned for the process lifetime; the next launch serves the new version.
        assert execute(backend, catalog) == event
        assert backend.pinned_catalog().snapshot == snapshot
        relaunched = workspace_backend(layout)
        try:
            served = execute(relaunched, catalog)
            assert isinstance(served, CatalogEvent) and served.reference == newer
            assert served.catalog.prompts[1].text == "An edited synthetic sentence."
        finally:
            relaunched.index.close()
    finally:
        backend.index.close()


def test_active_catalog_store_failures_are_named_and_pin_nothing(tmp_path: Path) -> None:
    from test_recording_takes import store_suite

    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    reference, _ = activate_catalog(layout, store_suite(2))
    snapshot_path = layout.root / reference.snapshot_path
    original = snapshot_path.read_bytes()
    catalog = CatalogCommand(operation_id="op-catalog")

    def attempt(code: str, match: str) -> None:
        backend = workspace_backend(layout)
        try:
            refused(backend, catalog, code, match)
            assert backend._pinned_catalog is None
        finally:
            backend.index.close()

    snapshot_path.write_bytes(original.replace(b"number 1.", b"number 9."))
    attempt(
        "CORPUS.SNAPSHOT_MISMATCH",
        "active catalog snapshot of subject synthetic-subject is altered or unsafe",
    )
    snapshot_path.unlink()
    attempt("CORPUS.SNAPSHOT_MISMATCH", "is missing")
    snapshot_path.write_bytes(original)
    snapshot_path.chmod(0o600)
    corpus = layout.root / "provenance/corpus"
    corpus.chmod(0o755)
    try:
        attempt("RECORDING.GATE_NOT_READY", "corpus store cannot serve the active catalog")
    finally:
        corpus.chmod(0o700)
    backend = workspace_backend(layout)
    try:
        event = execute(backend, catalog)
        assert isinstance(event, CatalogEvent) and event.reference == reference
    finally:
        backend.index.close()


def test_long_store_messages_are_bounded_on_the_wire() -> None:
    from cole_voice_lab.corpus_store import CorpusStoreError
    from cole_voice_lab.recording_service import _active_catalog_failure

    for code in ("CORPUS.STORE_MISSING", "CORPUS.STORE_UNAVAILABLE"):
        failure = _active_catalog_failure(CorpusStoreError(code, "x" * 5000), SUBJECT)
        assert len(failure.recording_error.message) == 1024
        assert failure.recording_error.message.endswith("...")


def test_open_context_opens_only_the_pinned_catalog_by_reference(tmp_path: Path) -> None:
    from test_recording_takes import DEVICE, store_suite

    from cole_voice_lab.recording_contracts import (
        RECORDING_GROUP_V2,
        ContextEvent,
        HistoryCommand,
    )

    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    suite = store_suite(3)
    first, _ = activate_catalog(layout, suite)
    with open_corpus_store(layout, create=False) as corpus:
        corpus.edit_prompt(
            "spoken-0000",
            text="A kept synthetic sentence.",
            keep_recordings=True,
            allow_rewording=True,
        )
        kept = corpus.publish_version("synthetic-corpus")
        corpus.activate(SUBJECT, kept)
        snapshot = corpus.materialize(kept)
    assert snapshot.prompt_aliases
    backend = workspace_backend(layout)
    try:
        embedded = OpenContextCommand(
            operation_id="context-embedded",
            subject_id=SUBJECT,
            catalog=snapshot.suite,
            catalog_sha256=kept.catalog_sha256,
            capture_device=DEVICE,
            capture_host="host",
        )
        refused(backend, embedded, "RECORDING.INVALID_MESSAGE", "continue-legacy-recording")
        assert backend._pinned_catalog is None
        refused(
            backend,
            referenced_context(first),
            "RECORDING.STALE_REVISION",
            "names catalog synthetic-corpus version 1 .* serves synthetic-corpus version 2",
        )
        forged = kept.model_copy(update={"snapshot_sha256": "f" * 64})
        refused(
            backend, referenced_context(forged), "RECORDING.STALE_REVISION", "request the catalog"
        )
        assert not any((layout.root / "provenance").rglob("group-*.json"))
        command = referenced_context(kept)
        event = execute(backend, command)
        assert isinstance(event, ContextEvent)
        assert event.group.schema_version == RECORDING_GROUP_V2
        assert event.group.catalog is None and event.group.catalog_reference == kept
        assert event.cursor.selection.prompt_id == "spoken-0000"
        assert execute(backend, command) == event
        # The cursor branch registers the same aliased snapshot the context registered.
        current = execute(backend, SnapshotCommand())
        assert isinstance(current, ContextEvent) and current.history is not None
        assert current.history.catalog_sha256 == kept.catalog_sha256
        history = execute(backend, HistoryCommand(catalog_sha256=kept.catalog_sha256))
        assert isinstance(history, RecordingHistoryPage)
        assert history.total_prompt_count == 3
        refused(
            backend,
            command.model_copy(update={"subject_id": "another-subject"}),
            "RECORDING.STALE_REVISION",
            "workspace subject",
        )
    finally:
        backend.index.close()


@pytest.mark.parametrize("direction", ["physical-test-on-standard", "standard-on-marked"])
def test_catalog_snapshot_open_and_continue_legacy_refuse_a_root_purpose_mismatch_first(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, direction: str
) -> None:
    from recording_fixtures import synthetic_physical_test_catalog
    from test_recording_takes import store_suite

    from cole_voice_lab.diagnostic_roots import initialize_diagnostic_root
    from cole_voice_lab.recording_contracts import ContinueLegacyRecordingCommand

    if direction == "physical-test-on-standard":
        subject = SUBJECT
        layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
        catalog = synthetic_physical_test_catalog()
        reference, _ = activate_catalog(layout, catalog, catalog_id=catalog.suite_id)
        refusal = "physical-test catalogs"
    else:
        subject = "cole-physical-diagnostic-synthetic"
        layout, _ = initialize_diagnostic_root(tmp_path.resolve() / "assets", subject_id=subject)
        catalog = store_suite(3)
        reference, _ = activate_catalog(layout, catalog, subject_id=subject)
        refusal = "marked for physical tests"
    backend = RecordingWorkspaceBackend(layout, subject, discover=False)
    try:

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("the root refusal must precede replay, gates and groups")

        for name in ("context_replay", "prepare_corpus_gate", "open_context", "open_group"):
            monkeypatch.setattr(backend.store, name, forbidden)
        monkeypatch.setattr(backend.index, "register_catalog", forbidden)
        context = referenced_context(
            reference, operation_id="physical-open", subject_id=subject, new_setup=True
        )
        embedded = context.model_dump() | {
            "catalog_reference": None,
            "catalog": catalog,
            "catalog_sha256": canonical_sha256(catalog),
        }
        commands: list[Any] = [
            CatalogCommand(operation_id="physical-catalog"),
            # Without a cursor the snapshot would report the refused catalog's digest.
            SnapshotCommand(),
            context,
            ContinueLegacyRecordingCommand(
                operation_id="physical-open",
                session_id="legacy-session",
                session_evidence_sha256="1" * 64,
                context=OpenContextCommand.model_validate(embedded),
            ),
        ]
        for command in commands:
            refused(backend, command, "RECORDING.GATE_NOT_READY", refusal)
        assert not any((layout.root / "provenance").rglob("group-*.json"))
    finally:
        backend.index.close()


def test_a_corpus_sized_catalog_reply_fits_one_control_frame(tmp_path: Path) -> None:
    from test_recording_contracts import CORPUS_SHAPED_PROMPT_COUNT, corpus_shaped_catalog

    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    suite = corpus_shaped_catalog()
    reference, _ = activate_catalog(layout, suite)
    backend = workspace_backend(layout)
    service = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=backend,
    )
    try:
        prepared(service)
        assert service.submit(request(CatalogCommand(operation_id="op-catalog"))) is None
        (response,) = await_responses(service)
        assert isinstance(response.data, CatalogEvent) and response.error is None
        assert response.data.reference == reference and response.meta.take_id is None
        assert len(response.data.catalog.prompts) == CORPUS_SHAPED_PROMPT_COUNT
        assert len(recording_frame_bytes(response)) <= MAX_CONTROL_FRAME_BYTES * 3 // 4
        assert not service.closed
    finally:
        service.close()
        backend.index.close()


def test_an_active_catalog_one_byte_over_the_frame_budget_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from test_recording_contracts import catalog_of_exactly

    from cole_voice_lab import corpus_store
    from cole_voice_lab.recording_contracts import MAX_CATALOG_SUITE_BYTES

    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    exact = catalog_of_exactly(MAX_CATALOG_SUITE_BYTES)
    pad = exact.prompts[-1]
    assert pad.text is not None
    shaped = tuple(prompt.prompt_id for prompt in exact.prompts[:-1])
    with open_corpus_store(layout, create=True) as corpus:
        corpus.import_prompt_suite(exact, source="manual")
        corpus.add_prompt(
            "shape-pae",
            text=pad.text + "p",
            category="neutral",
            style=pad.style,
            expected_seconds=pad.expected_seconds,
            source="manual",
        )
        corpus.create_catalog(exact.suite_id, purpose=exact.purpose)
        corpus.create_catalog("oversized", suite_id=exact.suite_id, purpose=exact.purpose)
        fitting = corpus.publish_version(exact.suite_id, prompt_ids=(*shaped, pad.prompt_id))
        # A version one byte over, as a store without the publish-time bound could hold.
        with monkeypatch.context() as lifted:
            lifted.setattr(corpus_store, "MAX_CATALOG_SUITE_BYTES", MAX_CATALOG_SUITE_BYTES + 1)
            oversized = corpus.publish_version("oversized", prompt_ids=(*shaped, "shape-pae"))
        corpus.activate(SUBJECT, oversized)
    backend = workspace_backend(layout)
    service = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=backend,
    )
    try:
        prepared(service)
        assert service.submit(request(CatalogCommand(operation_id="op-oversized"))) is None
        (response,) = await_responses(service, timeout=30)
        assert response.data is None and response.error is not None
        assert response.error.code == "RECORDING.GATE_NOT_READY"
        assert (
            f"active catalog oversized version 1 is {MAX_CATALOG_SUITE_BYTES + 1} bytes with "
            f"{len(exact.prompts)} prompts" in response.error.message
        )
        assert "voice-lab catalog activate" in response.error.message
        # Refused by name, not by closing the session; nothing was pinned.
        assert not service.closed and backend._pinned_catalog is None
        for data in (SnapshotCommand(), referenced_context(oversized)):
            refused(backend, data, "RECORDING.GATE_NOT_READY", "the app receives in one message")
        assert not any((layout.root / "provenance").rglob("group-*.json"))
        with open_corpus_store(layout, create=False) as corpus:
            corpus.activate(SUBJECT, fitting)
        # A suite exactly at the budget is served whole in one frame.
        assert (
            service.submit(request(CatalogCommand(operation_id="op-fitting"), request_id="r-2"))
            is None
        )
        (served,) = await_responses(service, timeout=30)
        assert isinstance(served.data, CatalogEvent) and served.data.reference == fitting
        assert len(recording_frame_bytes(served)) <= MAX_CONTROL_FRAME_BYTES
        assert not service.closed
    finally:
        service.close()
        backend.index.close()


def test_service_launch_names_its_catalog_digest_outside_workspace_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cole_voice_lab.recording_service import main

    root = tmp_path.resolve()
    layout = AssetLayout.initialize(root / "assets")
    write_canonical_json(root / "service-build.json", recording_service_build_identity())
    write_canonical_json(root / "client-build.json", build_identity())
    base = [
        "--asset-root",
        str(layout.root),
        "--process-generation",
        "generation-1",
        "--build-identity",
        str(root / "service-build.json"),
        "--expected-client-build",
        str(root / "client-build.json"),
    ]
    for extra in (
        [],
        ["--recording-group-id", "group-synthetic"],
        ["--catalog-sha256", "not-a-digest"],
        ["--native-workspace", "--recording-group-id", "group-synthetic"],
    ):
        assert main([*base, *extra]) == 2
        report = json.loads(capsys.readouterr().err)
        assert report["error_code"] == "RECORDING.INVALID_MESSAGE"
    assert not (layout.root / "provenance/corpus").exists()


def test_a_workspace_service_refuses_a_launch_catalog_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The workspace names its pinned catalog; a launch digest would go unchecked."""
    from cole_voice_lab.cli import main as cli_main
    from cole_voice_lab.recording_service import main

    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    base = [
        "--asset-root",
        str(layout.root),
        "--process-generation",
        "generation-1",
        "--build-identity",
        "service.json",
        "--expected-client-build",
        "client.json",
        "--native-workspace",
        "--catalog-sha256",
        "a" * 64,
    ]
    for launch in (main, lambda args: cli_main(["recording", "service", *args])):
        with pytest.raises(SystemExit) as refused:
            launch(base)
        assert refused.value.code == 2
        error = capsys.readouterr().err
        assert "argument --catalog-sha256: not allowed with argument --native-workspace" in error
    # Nothing was opened or created: argparse refuses before the service starts.
    assert not (layout.root / "provenance/corpus").exists()
    assert not (layout.root / "provenance/recording-takes").exists()


def test_real_workspace_service_delivers_its_catalog_before_a_referenced_context(
    tmp_path: Path,
) -> None:
    from test_recording_navigation import ServiceProcess
    from test_recording_takes import store_suite

    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    reference, snapshot = activate_catalog(layout, store_suite(3))
    service = ServiceProcess(tmp_path.resolve(), layout, "catalog-generation")
    try:
        delivered = service.request({"command": "catalog", "operation_id": "op-catalog"})
        assert delivered is not None and delivered.error is None
        assert isinstance(delivered.data, CatalogEvent)
        assert delivered.data.reference == reference
        assert delivered.data.catalog == snapshot.suite
        page = service.request({"command": "snapshot"})
        assert page is not None and isinstance(page.data, RecordingHistoryPage)
        assert page.data.catalog_sha256 == reference.catalog_sha256
        opened = service.request(referenced_context(reference).model_dump())
        assert opened is not None and opened.error is None
        assert opened.data is not None and opened.data.event == "context"
        assert opened.data.group.catalog_reference == reference
        shutdown = service.request({"command": "shutdown"})
        assert shutdown is not None and isinstance(shutdown.data, AcknowledgedEvent)
        assert service.process.wait(timeout=5) == 0
    finally:
        service.close()


@pytest.mark.parametrize("digest", [True, False])
def test_cli_service_route_preserves_explicit_launch_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, digest: bool
) -> None:
    from cole_voice_lab.cli import main

    calls: list[Any] = []
    monkeypatch.setattr(
        "cole_voice_lab.recording_service.main", lambda args: calls.append(args) or 0
    )
    args = [
        "--asset-root",
        str(tmp_path.resolve()),
        *(["--catalog-sha256", "a" * 64] if digest else []),
        "--process-generation",
        "generation-1",
        "--build-identity",
        "service.json",
        "--expected-client-build",
        "client.json",
        *([] if digest else ["--native-workspace", "--subject-id", SUBJECT]),
    ]
    assert main(["recording", "service", *args]) == 0
    assert calls == [args]


def test_build_identity_fingerprints_the_corpus_store_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cole_voice_lab import recording_service

    baseline = recording_service_build_identity()
    resources = recording_service.corpus_store_migration_resources()
    assert len(resources) == 1 and resources[0][0].startswith("migrations/2026091610")
    monkeypatch.setattr(
        recording_service,
        "corpus_store_migration_resources",
        lambda: ((resources[0][0], "0" * 64, resources[0][2]),),
    )
    changed = recording_service_build_identity()
    assert changed.source_sha256 != baseline.source_sha256
    assert changed.executable_sha256 == baseline.executable_sha256


def test_backends_name_a_missing_or_altered_group_snapshot(tmp_path: Path) -> None:
    from test_recording_takes import publish_catalog, store_suite

    from cole_voice_lab.recording_contracts import ContextEvent, ReserveTakeCommand
    from cole_voice_lab.recording_service import RecordingTakesBackend
    from cole_voice_lab.recording_takes import RecordingTakeStore

    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    reference, snapshot = publish_catalog(layout, store_suite(2))
    store = RecordingTakeStore(layout)
    context = store.open_context(referenced_context(reference))
    takes = RecordingTakesBackend(store, context.group.group_id)
    reserve = ReserveTakeCommand(
        recorder_generation="helper-generation",
        operation_id="reserve-damaged",
        group_id=context.group.group_id,
        selection=context.cursor.selection,
    )
    path = layout.root / reference.snapshot_path
    original = path.read_bytes()

    def assert_named(state: str) -> None:
        backend = RecordingWorkspaceBackend(layout, "synthetic-subject", discover=False)
        try:
            with pytest.raises(RecordingServiceError, match=f"references is {state}") as failure:
                backend.execute(
                    request(SnapshotCommand(), revision=backend.state_revision),
                    RecordingCancellation(),
                )
            assert failure.value.recording_error.code == "CORPUS.SNAPSHOT_MISMATCH"
        finally:
            backend.index.close()
        # A store that has not verified the snapshot yet refuses the reservation too.
        takes.store = RecordingTakeStore(layout)
        with pytest.raises(RecordingServiceError, match=f"references is {state}") as failure:
            takes.execute(request(reserve, revision=takes.state_revision), RecordingCancellation())
        assert failure.value.recording_error.code == "CORPUS.SNAPSHOT_MISMATCH"
        assert not (
            layout.root / "provenance/recording-takes/.operations/reserve-damaged.json"
        ).exists()

    backend = RecordingWorkspaceBackend(layout, "synthetic-subject", discover=False)
    try:
        event = backend.execute(
            request(SnapshotCommand(), revision=backend.state_revision), RecordingCancellation()
        )
        assert isinstance(event, ContextEvent)
        assert event.group.catalog_reference == reference and event.group.catalog is None
        assert event.history is not None
        assert event.history.catalog_sha256 == reference.catalog_sha256
        assert backend.library is not None and backend.library.prompt_suite == snapshot.suite
    finally:
        backend.index.close()
    path.write_bytes(original.replace(b"number 1.", b"number 9."))
    assert_named("altered or unsafe")
    path.unlink()
    assert_named("missing")
