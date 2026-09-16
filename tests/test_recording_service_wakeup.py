"""The pipe server delivers a completed reply at once; closure and deadline paths stay bounded.

Every test pins the idle safety net far beyond its own bound, so a reply or an exit that
still depended on a timed poll would fail the test instead of arriving late.
"""

from __future__ import annotations

import os
import select
import threading
import time
from collections import deque

import pytest
from test_recording_service import (
    BlockingMetadataBackend,
    blocking_session,
    build_identity,
    handshake,
    recovery_request,
    request,
)

from cole_voice_lab.recording_contracts import (
    AcknowledgedEvent,
    RecordingResponse,
    ReliableEvent,
    ServiceRequest,
    ShutdownCommand,
    SnapshotCommand,
    recording_frame_bytes,
)
from cole_voice_lab.recording_service import (
    RecordingCancellation,
    RecordingFrameDecoder,
    RecordingServiceError,
    RecordingServiceSession,
    UnpreparedRecordingBackend,
    serve_recording_pipes,
)

IDLE_NET = "cole_voice_lab.recording_service.CONTROL_IDLE_WAKE_SECONDS"


class ReleasedMetadataBackend(UnpreparedRecordingBackend):
    """Each metadata call waits for its release and records the instant its reply left."""

    def __init__(self) -> None:
        super().__init__("a" * 64)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.completed_at: list[float] = []

    def execute(
        self, request: ServiceRequest, cancellation: RecordingCancellation
    ) -> ReliableEvent:
        self.entered.set()
        while not self.release.wait(0.01):
            cancellation.check()
        self.release.clear()
        self.completed_at.append(time.perf_counter())
        return AcknowledgedEvent(operation_id="op-recovery", action="recovered")


class InvalidReplyBackend(UnpreparedRecordingBackend):
    def execute(
        self, request: ServiceRequest, cancellation: RecordingCancellation
    ) -> ReliableEvent:
        return "not a reliable event"  # type: ignore[return-value]


class PipeServer:
    """serve_recording_pipes on its own thread; the test holds the peer ends of both pipes."""

    def __init__(self, session: RecordingServiceSession, *, stall_output: bool = False) -> None:
        self.session = session
        self.input_read, self.input_write = os.pipe()
        self.output_read, self.output_write = os.pipe()
        self.failures: list[BaseException] = []
        self.inbox: deque[RecordingResponse] = deque()
        self.decoder = RecordingFrameDecoder()
        self._input_open = True
        if stall_output:
            os.set_blocking(self.output_write, False)
            try:
                while True:
                    os.write(self.output_write, b"x" * 4096)
            except BlockingIOError:
                pass
        self.thread = threading.Thread(target=self._serve, name="pipe-server-under-test")
        self.thread.start()

    def _serve(self) -> None:
        try:
            serve_recording_pipes(self.session, self.input_read, self.output_write)
        except BaseException as error:
            self.failures.append(error)

    def send(self, message: ServiceRequest) -> None:
        os.write(self.input_write, recording_frame_bytes(message))

    def receive(self, timeout: float = 0.5) -> RecordingResponse:
        deadline = time.monotonic() + timeout
        while not self.inbox:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "reply did not arrive within its bound"
            if select.select([self.output_read], [], [], remaining)[0]:
                chunk = os.read(self.output_read, 65536)
                assert chunk, "reply pipe closed before the reply"
                self.inbox.extend(
                    RecordingResponse.model_validate_json(payload)
                    for payload in self.decoder.feed(chunk)
                )
        return self.inbox.popleft()

    def end_input(self) -> None:
        if self._input_open:
            self._input_open = False
            os.close(self.input_write)

    def finished(self, within: float) -> bool:
        self.thread.join(within)
        return not self.thread.is_alive()

    def close(self) -> None:
        self.end_input()
        self.thread.join(2)
        for descriptor in (self.input_read, self.output_read, self.output_write):
            os.close(descriptor)


def test_completed_reply_wakes_the_pipe_server_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(IDLE_NET, 30.0)
    backend = ReleasedMetadataBackend()
    server = PipeServer(blocking_session(backend))
    latencies: list[float] = []
    try:
        for index in range(20):
            server.send(recovery_request(f"wake-{index}"))
            assert backend.entered.wait(1)
            backend.entered.clear()
            backend.release.set()
            response = server.receive(timeout=0.5)
            received = time.perf_counter()
            assert response.meta.request_id == f"wake-{index}" and response.error is None
            latencies.append(received - backend.completed_at[-1])
    finally:
        server.close()
    assert server.failures == []
    latencies.sort()
    assert latencies[len(latencies) // 2] < 0.005, latencies
    assert latencies[-1] < 0.1, latencies


@pytest.mark.parametrize("ending", ["shutdown", "eof", "close"])
def test_closure_paths_end_the_server_without_a_reply_poll(
    monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    monkeypatch.setattr(IDLE_NET, 30.0)
    backend = BlockingMetadataBackend()
    server = PipeServer(blocking_session(backend))
    try:
        server.send(recovery_request())
        assert backend.entered.wait(1)
        started = time.monotonic()
        if ending == "shutdown":
            server.send(request(ShutdownCommand(), request_id="shutdown"))
            while True:
                reply = server.receive(timeout=1)
                if reply.meta.request_id == "shutdown":
                    assert isinstance(reply.data, AcknowledgedEvent)
                    assert reply.data.action == "shutdown"
                    break
        elif ending == "eof":
            server.end_input()
        else:
            server.session.close()
        assert server.finished(within=1.5), ending
        assert time.monotonic() - started < 1.5
        assert server.failures == [] and server.session.closed
    finally:
        server.close()


def test_output_deadline_holds_without_a_reply_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(IDLE_NET, 30.0)
    monkeypatch.setattr("cole_voice_lab.recording_service.CONTROL_OUTPUT_DEADLINE_SECONDS", 0.05)
    session = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=UnpreparedRecordingBackend("a" * 64),
    )
    server = PipeServer(session, stall_output=True)
    try:
        server.send(handshake())
        assert server.finished(within=1)
        assert len(server.failures) == 1 and isinstance(server.failures[0], RecordingServiceError)
        assert server.failures[0].recording_error.code == "RECORDING.PARENT_LOST"
    finally:
        server.close()


def test_invalid_backend_reply_ends_the_server_without_a_reply_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(IDLE_NET, 30.0)
    session = RecordingServiceSession(
        build=build_identity(),
        expected_client=build_identity(),
        process_generation="generation-1",
        backend=InvalidReplyBackend("a" * 64),
    )
    server = PipeServer(session)
    try:
        server.send(handshake())
        assert server.receive(timeout=1).error is None
        server.send(request(SnapshotCommand()))
        # The closure wakes the loop; with nothing left to write it ends as it always has.
        assert server.finished(within=1)
        assert server.failures == [] and session.closed
        assert session.terminal_error is not None
        assert session.terminal_error.recording_error.code == "RECORDING.INVALID_MESSAGE"
    finally:
        server.close()
