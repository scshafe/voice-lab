import Foundation
import RecordingContracts
import RecordingCore
@testable import RecordingUI
import Testing

/// A capture terminal without a registered seal queues one explicit service recheck, then
/// returns to Ready. Replies are injected; no process, microphone or file is opened.
@Suite(.serialized)
struct RecordingUnsealedTerminalTests {
    @MainActor private final class Rechecks {
        var takeIDs: [String] = []
        var recordEnabledDuringRequest: [Bool] = []
    }

    private func envelope(data: RecordingJSON, error: RecordingJSON = .null, takeID: String = "synthetic-take",
                          generation: String = "synthetic-generation") throws -> RecordingWireEnvelope {
        let body: RecordingJSON = .object(["data": data, "error": error,
            "meta": .object(["protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("start-request"),
                "process_generation": .string(generation), "state_revision": .integer(2), "event_sequence": .integer(3),
                "take_id": .string(takeID)])])
        // Model fixtures bypass only wire-shape validation; shared conformance suites own it.
        return try RecordingWireEnvelope.decode(body.transportData(), schema: Data(#"{"type":"object"}"#.utf8))
    }

    private func helperError(_ code: String, _ message: String, takeID: String = "synthetic-take",
                             generation: String = "synthetic-generation") throws -> RecordingWireEnvelope {
        try envelope(data: .null, error: .object(["code": .string(code), "message": .string(message), "retryable": .boolean(false)]),
                     takeID: takeID, generation: generation)
    }

    private func terminal(frames: Int64, takeID: String = "synthetic-take", generation: String = "synthetic-generation") throws -> RecordingWireEnvelope {
        try envelope(data: .object(["event": .string("recorder-state"), "take_id": .string(takeID), "transport": .string("error"),
                                    "written_frames": .integer(frames), "seal": .null]), takeID: takeID, generation: generation)
    }

    private func recoveryReply(operation: String, takeID: String, code: String, message: String) throws -> RecordingWireEnvelope {
        try envelope(data: .object(["event": .string("recovery"), "operation_id": .string(operation), "take_id": .string(takeID),
            "page": .object(["entries": .array([.object(["take_id": .string(takeID), "status": .string("partial"),
                "recovered_seal": .boolean(false),
                "error": .object(["code": .string(code), "message": .string(message), "retryable": .boolean(false)])])]),
                "next_take_id": .null])]), takeID: takeID)
    }

    @MainActor private func capturing(_ rechecks: Rechecks, code: String = "RECORDING.CANCELLED",
                                      message: String = "Capture stopped before any complete PCM frames arrived") -> RecordingWorkspaceModel {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .recording))
        model.startOperationID = "synthetic-start"
        model.inputStartDispatched = true
        model.state.take = .object(["reservation": .object(["take_id": .string("synthetic-take"), "operation_id": .string("synthetic-start")]),
                                    "decision": .string("draft"), "analysis": .string("pending"), "seal": .null])
        model.state.contextReady = true; model.state.deviceReady = true
        model.state.storageReady = true; model.state.serviceReady = true
        model.recoverRequest = { [weak model] operation, takeID in
            rechecks.takeIDs.append(takeID)
            rechecks.recordEnabledDuringRequest.append(model?.canRecord ?? true)
            return try self.recoveryReply(operation: operation, takeID: takeID, code: code, message: message)
        }
        return model
    }

    /// Await the one scheduled recheck; its absence is itself a failure.
    @MainActor private func settle(_ model: RecordingWorkspaceModel) async throws {
        let task = try #require(model.unsealedRecheckTask)
        await task.value
    }

    @MainActor private func stoppedBeforeAudio(_ rechecks: Rechecks) throws -> RecordingWorkspaceModel {
        let model = capturing(rechecks)
        model.stop()
        model.handleRecorder(try helperError("RECORDING.CANCELLED", "Capture stopped before any complete PCM frames arrived"))
        model.handleRecorder(try terminal(frames: 0))
        return model
    }

    @Test @MainActor func zeroFrameStopReturnsReadyAndEnablesRecordOnlyAfterOneRecheck() async throws {
        let rechecks = Rechecks()
        let model = try stoppedBeforeAudio(rechecks)
        #expect(model.state.phase == .ready)
        #expect(model.state.errorCode == nil)
        #expect(model.state.status == RecordingWorkspaceModel.cancelledBeforeAudioStatus)
        #expect(model.captureTerminalNotice == model.state.status)
        #expect(model.state.unsealedRecheckPending && !model.canRecord)
        #expect(model.state.recordUnavailableReason == "Checking the stopped take before the next recording")
        // The never-sealed reservation is not a draft: Retake and Take actions stay suppressed.
        #expect(model.state.reservationUnsealed)
        try await settle(model)
        #expect(rechecks.takeIDs == ["synthetic-take"])
        #expect(rechecks.recordEnabledDuringRequest == [false])
        #expect(model.state.phase == .ready && !model.state.unsealedRecheckPending && model.canRecord)
        #expect(model.state.status == RecordingWorkspaceModel.cancelledBeforeAudioStatus)
        // The service finding is retained unchanged; only its presentation is not an alarm.
        #expect(model.recoveryIncompleteRows.map(\.errorCode) == ["RECORDING.CANCELLED"])
        #expect(model.recoveryAttentionRows.isEmpty && model.recoveryStatusLine == nil && !model.canOpenRecovery)
    }

    @Test @MainActor func duplicateTerminalAfterReadyStaysReadyWithoutASecondRecheck() async throws {
        let rechecks = Rechecks()
        let model = try stoppedBeforeAudio(rechecks)
        try await settle(model)
        // A resnapshot or Stop reply repeats the terminal; neither may restore the dead end.
        model.handleRecorder(try helperError("RECORDING.CANCELLED", "Capture stopped before any complete PCM frames arrived"))
        model.handleRecorder(try terminal(frames: 0))
        model.handleRecorder(try helperError("RECORDING.WRITE_FAILED", "Recorder completion failed; partial evidence requires recovery"))
        model.handleRecorder(try terminal(frames: 0))
        #expect(model.state.phase == .ready && model.state.errorCode == nil && model.canRecord)
        #expect(model.state.status == RecordingWorkspaceModel.cancelledBeforeAudioStatus)
        #expect(model.unsealedRecheckTask == nil && rechecks.takeIDs == ["synthetic-take"])
    }

    @Test(arguments: [("RECORDING.WRITE_FAILED", Int64(0)), ("RECORDING.CANCELLED", Int64(480))])
    @MainActor func unsealedPartialRechecksThenReadyWithItsFindingVisible(code: String, frames: Int64) async throws {
        let rechecks = Rechecks()
        let message = "Recorder completion failed; partial evidence requires recovery"
        let model = capturing(rechecks, code: code, message: message)
        model.stop()
        model.handleRecorder(try helperError(code, message))
        model.handleRecorder(try terminal(frames: frames))
        #expect(model.state.phase == .recoverableError && model.state.unsealedRecheckPending && !model.canRecord)
        try await settle(model)
        #expect(rechecks.takeIDs == ["synthetic-take"])
        #expect(model.state.phase == .ready && model.canRecord)
        #expect(model.state.errorCode == code && model.state.status == message)
        // The named failure outranks background progress and its finding stays reachable.
        #expect(model.captureTerminalNotice == model.state.status)
        #expect(model.recoveryAttentionRows.map(\.takeID) == ["synthetic-take"] && model.canOpenRecovery)
        model.openRecoverySheet()
        #expect(model.recoverySheetOpen)
    }

    @Test @MainActor func rejectedStartAndFailedSealRegistrationRecheckThenReady() async throws {
        let rechecks = Rechecks()
        let rejected = capturing(rechecks)
        rejected.state.phase = .starting
        rejected.handleStartFailure(RecordingContractError(code: "RECORDING.OWNERSHIP_CONFLICT", message: "Capture ownership is held by another observer"))
        #expect(rejected.state.phase == .recoverableError && !rejected.canRecord)
        try await settle(rejected)
        #expect(rechecks.takeIDs == ["synthetic-take"])
        #expect(rejected.state.phase == .ready && rejected.canRecord && rejected.state.errorCode == "RECORDING.OWNERSHIP_CONFLICT")

        let registration = capturing(rechecks)
        registration.state.phase = .stopping; registration.state.registrationPending = true
        registration.handleUnsealedFailure(RecordingContractError(code: "RECORDING.WRITE_FAILED", message: "recording metadata could not be synced"),
                                           takeID: "synthetic-take", captureReleased: true)
        #expect(registration.state.phase == .recoverableError && !registration.state.registrationPending && !registration.canRecord)
        try await settle(registration)
        #expect(rechecks.takeIDs == ["synthetic-take", "synthetic-take"])
        #expect(registration.state.phase == .ready && registration.canRecord)
    }

    @Test @MainActor func failedRecheckStillReturnsReadyWithTheFailureReachable() async throws {
        let model = capturing(Rechecks())
        model.recoverRequest = { _, _ in throw RecordingContractError(code: "RECORDING.CANCELLED", message: "Local process channel was closed") }
        model.stop()
        model.handleRecorder(try terminal(frames: 480))
        try await settle(model)
        #expect(model.state.phase == .ready && model.canRecord)
        #expect(model.recoverySupervisorError == "Local process channel was closed" && model.canOpenRecovery)
    }

    /// End a recorder event stream the way a closed channel does, through the model's own consumer.
    @MainActor private func closeChannel(_ model: RecordingWorkspaceModel, _ error: RecordingContractError,
                                         buffered: [RecordingWireEnvelope] = []) async {
        let (events, continuation) = AsyncThrowingStream.makeStream(of: RecordingWireEnvelope.self, throwing: Error.self)
        let consumer = model.consume(events) { $0.handleRecorder($1) }
        for event in buffered { continuation.yield(event) }
        continuation.finish(throwing: error)
        await consumer.value
    }

    /// A recorder channel closure (request deadline or helper exit) fails the pending Stop or
    /// Start request and separately ends the event stream. Whichever lands first, the recheck
    /// still runs once, but Record is never offered over the closed channel.
    @Test(arguments: [(true, "consumer-first"), (true, "request-first"), (true, "after-recheck"),
                      (false, "consumer-first"), (false, "request-first"), (false, "after-recheck")])
    @MainActor func closedRecorderChannelKeepsRecordUnavailableAfterTheRecheck(duringStop: Bool, order: String) async throws {
        let rechecks = Rechecks()
        let model = capturing(rechecks)
        let closed = RecordingContractError(code: "RECORDING.CANCELLED", message: "Local process request exceeded its deadline; channel closed for recovery")
        if duringStop { model.stop() } else { model.state.phase = .starting }
        if order == "consumer-first" { await closeChannel(model, closed) }
        // Hold the recheck behind a page load so the stream failure lands while it is pending.
        if order == "request-first" { model.recoveryInventoryLoading = true }
        if duringStop { model.handleUnsealedFailure(closed, takeID: "synthetic-take") } else { model.handleStartFailure(closed) }
        #expect(model.state.phase == .recoverableError && model.state.unsealedRecheckPending && !model.canRecord)
        if order == "request-first" { await closeChannel(model, closed); model.recoveryInventoryLoading = false }
        try await settle(model)
        if order == "after-recheck" { await closeChannel(model, closed) }
        #expect(rechecks.takeIDs == ["synthetic-take"] && !model.state.unsealedRecheckPending)
        #expect(!model.canRecord)
        #expect(model.processChannelFailed && model.state.phase == .recoverableError)
        #expect(model.state.errorCode == "RECORDING.CANCELLED" && model.state.status == closed.message)
        #expect(model.recordUnavailableReason == RecordingWorkspaceModel.processChannelFailedReason)
        // Record reserves nothing: the start operation and take stay the closed capture's.
        model.record()
        #expect(model.startOperationID == "synthetic-start" && model.state.phase == .recoverableError)
    }

    @Test @MainActor func zeroFrameTerminalBufferedBeforeChannelClosureDoesNotReturnReady() async throws {
        let rechecks = Rechecks()
        let model = capturing(rechecks)
        model.recoveryInventoryLoading = true
        model.stop()
        await closeChannel(model, RecordingContractError(code: "RECORDING.PARENT_LOST", message: "Peer control channel ended"), buffered: [
            try helperError("RECORDING.CANCELLED", "Capture stopped before any complete PCM frames arrived"), try terminal(frames: 0)])
        #expect(model.unsealedRecheckQueued && model.state.unsealedRecheckPending)
        model.recoveryInventoryLoading = false
        try await settle(model)
        #expect(rechecks.takeIDs == ["synthetic-take"])
        #expect(!model.canRecord)
        #expect(model.state.phase == .recoverableError && model.state.errorCode == "RECORDING.PARENT_LOST")
    }

    @Test @MainActor func laterReadyPublicationCannotOfferRecordOverAClosedChannel() async {
        let model = capturing(Rechecks())
        model.state.phase = .starting; model.inputStartDispatched = false
        let closed = RecordingContractError(code: "RECORDING.CANCELLED", message: "Local process channel was closed")
        await closeChannel(model, closed)
        // A start failure before input dispatch returns to Ready without any recheck.
        model.handleStartFailure(closed)
        #expect(model.state.phase == .ready && model.state.canRecord)
        #expect(!model.canRecord && model.recordUnavailableReason == RecordingWorkspaceModel.processChannelFailedReason)
        #expect(model.unsealedRecheckTask == nil)
    }

    @Test @MainActor func foreignGenerationTakeOrReplacedStartIsIgnored() throws {
        let rechecks = Rechecks()
        let model = capturing(rechecks)
        model.handleRecorder(try terminal(frames: 0, generation: "old-generation"))
        model.handleRecorder(try terminal(frames: 0, takeID: "old-take"))
        model.startOperationID = "new-start"
        model.handleRecorder(try terminal(frames: 0))
        #expect(model.state.phase == .recording && model.state.canStop)
        #expect(model.unsealedRecheckTask == nil && !model.state.unsealedRecheckPending && rechecks.takeIDs.isEmpty)
    }

    @Test @MainActor func recheckDuringInventoryLoadIsQueuedNotDropped() async throws {
        let rechecks = Rechecks()
        let model = capturing(rechecks)
        model.recoveryInventoryLoading = true
        model.stop()
        model.handleRecorder(try helperError("RECORDING.CANCELLED", "Capture stopped before any complete PCM frames arrived"))
        model.handleRecorder(try terminal(frames: 0))
        #expect(model.unsealedRecheckTask == nil && model.unsealedRecheckQueued && !model.canRecord)
        model.recoveryInventoryLoading = false
        try await settle(model)
        #expect(rechecks.takeIDs == ["synthetic-take"])
        #expect(!model.unsealedRecheckQueued && model.canRecord)
    }

    @Test @MainActor func pageLoadStartingBeforeTheScheduledRecheckRequeuesIt() async throws {
        let rechecks = Rechecks()
        let model = try stoppedBeforeAudio(rechecks)
        let scheduled = try #require(model.unsealedRecheckTask)
        model.recoveryInventoryLoading = true
        await scheduled.value
        #expect(rechecks.takeIDs.isEmpty && model.unsealedRecheckQueued && !model.canRecord)
        model.recoveryInventoryLoading = false
        try await settle(model)
        #expect(rechecks.takeIDs == ["synthetic-take"] && model.canRecord)
    }
}
