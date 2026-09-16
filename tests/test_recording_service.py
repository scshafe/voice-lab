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

from cole_voice_lab.canonical import canonical_json_bytes, write_canonical_json
from cole_voice_lab.errors import ContractError, SecurityError
from cole_voice_lab.paths import AssetLayout
from cole_voice_lab.recording_contracts import (
    MAX_CONTROL_FRAME_BYTES,
    MAX_TELEMETRY_FRAME_BYTES,
    AcknowledgedEvent,
    CancelCommand,
    HandshakeCommand,
    HistoryCommand,
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


def await_responses(session: RecordingServiceSession, count: int = 1) -> list[RecordingResponse]:
    deadline = time.monotonic() + 2
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


def test_open_and_continue_legacy_refuse_physical_test_before_replay_or_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from recording_fixtures import synthetic_physical_test_catalog

    from cole_voice_lab.canonical import canonical_sha256
    from cole_voice_lab.models import CaptureDevice
    from cole_voice_lab.recording_contracts import (
        ContinueLegacyRecordingCommand,
        OpenContextCommand,
    )
    from cole_voice_lab.recording_service import RecordingWorkspaceBackend

    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    backend = RecordingWorkspaceBackend(layout, "synthetic-subject", "0" * 64, discover=False)
    try:

        def forbidden(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("physical-test refusal must precede replay, gates and groups")

        for name in ("context_replay", "prepare_corpus_gate", "open_context", "open_group"):
            monkeypatch.setattr(backend.store, name, forbidden)
        monkeypatch.setattr(backend.index, "register_catalog", forbidden)
        catalog = synthetic_physical_test_catalog()
        context = OpenContextCommand(
            operation_id="physical-open",
            subject_id="synthetic-subject",
            catalog=catalog,
            catalog_sha256=canonical_sha256(catalog),
            capture_device=CaptureDevice(
                backend="coreaudio", device_id="synthetic", display_name="Synthetic"
            ),
            capture_host="host",
            new_setup=True,
        )
        commands: list[Any] = [
            context,
            ContinueLegacyRecordingCommand(
                operation_id="physical-open",
                session_id="legacy-session",
                session_evidence_sha256="1" * 64,
                context=context,
            ),
        ]
        for command in commands:
            with pytest.raises(RecordingServiceError, match="physical-test catalogs") as failure:
                backend.execute(
                    request(command, revision=backend.state_revision), RecordingCancellation()
                )
            assert failure.value.recording_error.code == "RECORDING.GATE_NOT_READY"
        assert not any((layout.root / "provenance").rglob("group-*.json"))
    finally:
        backend.index.close()


def test_cli_service_route_preserves_explicit_launch_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from cole_voice_lab.cli import main

    calls: list[Any] = []
    monkeypatch.setattr(
        "cole_voice_lab.recording_service.main", lambda args: calls.append(args) or 0
    )
    args = [
        "--asset-root",
        str(tmp_path.resolve()),
        "--catalog-sha256",
        "a" * 64,
        "--process-generation",
        "generation-1",
        "--build-identity",
        "service.json",
        "--expected-client-build",
        "client.json",
    ]
    assert main(["recording", "service", *args]) == 0
    assert calls == [args]


def test_backends_name_a_missing_or_altered_group_snapshot(tmp_path: Path) -> None:
    from test_recording_takes import DEVICE, publish_catalog, store_suite

    from cole_voice_lab.recording_contracts import (
        ContextEvent,
        OpenContextCommand,
        ReserveTakeCommand,
    )
    from cole_voice_lab.recording_service import RecordingTakesBackend, RecordingWorkspaceBackend
    from cole_voice_lab.recording_takes import RecordingTakeStore

    layout = AssetLayout.initialize(tmp_path.resolve() / "assets")
    reference, snapshot = publish_catalog(layout, store_suite(2))
    store = RecordingTakeStore(layout)
    context = store.open_context(
        OpenContextCommand(
            operation_id="context-referenced",
            subject_id="synthetic-subject",
            catalog=snapshot.suite,
            catalog_sha256=reference.catalog_sha256,
            capture_device=DEVICE,
            capture_host="host",
        ),
        catalog_reference=reference,
    )
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
        backend = RecordingWorkspaceBackend(
            layout, "synthetic-subject", reference.catalog_sha256, discover=False
        )
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

    backend = RecordingWorkspaceBackend(
        layout, "synthetic-subject", reference.catalog_sha256, discover=False
    )
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
