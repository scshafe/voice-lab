import Foundation
import RecordingContracts
import RecordingCore
@testable import RecordingUI
import Testing

@Suite(.serialized)
struct RecordingDeviceReconnectTests {
    private func event(terminal: Bool, takeID: String = "synthetic-take", generation: String = "synthetic-generation") throws -> RecordingWireEnvelope {
        let body: RecordingJSON = .object([
            "data": terminal ? .object(["event": .string("recorder-state"), "take_id": .string(takeID), "transport": .string("error"), "written_frames": .integer(480), "seal": .null]) : .null,
            "error": terminal ? .null : .object(["code": .string("RECORDING.DEVICE_UNAVAILABLE"), "message": .string("Native microphone UID disappeared"), "retryable": .boolean(false)]),
            "meta": .object(["protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("start-request"), "process_generation": .string(generation), "state_revision": .integer(2), "event_sequence": .integer(terminal ? 3 : 2), "take_id": .string(takeID)])])
        // Model fixtures bypass only wire-shape validation; shared conformance suites own it.
        return try RecordingWireEnvelope.decode(body.transportData(), schema: Data(#"{"type":"object"}"#.utf8))
    }

    @MainActor private func capturing() -> RecordingWorkspaceModel {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .recording))
        model.startOperationID = "synthetic-start"
        model.state.take = .object(["reservation": .object(["operation_id": .string("synthetic-start")]), "decision": .string("draft")])
        model.state.contextReady = true; model.state.deviceReady = true
        return model
    }

    @Test @MainActor func deviceErrorKeepsStopUntilTerminalThenOffersExplicitReconnect() throws {
        let model = capturing()
        let selected = model.state.selection
        let originalTake = model.state.take
        model.handleRecorder(try event(terminal: false))
        #expect(model.state.canStop)
        #expect(!model.audioReconnectRequired)
        model.handleRecorder(try event(terminal: true))
        #expect(model.state.phase == .recoverableError)
        #expect(model.canReconnectAudio)
        #expect(!model.canRecord && !model.state.deviceReady && !model.state.contextReady)
        #expect(model.state.errorCode == "RECORDING.DEVICE_UNAVAILABLE")
        #expect(model.state.status == "Native microphone UID disappeared")
        #expect(model.audioReconnectCause.contextDescription == "Audio reconnected after microphone interruption")
        #expect(model.state.selection == selected && model.state.take == originalTake)
        // Device loss keeps its own explicit Reconnect path rather than the unsealed recheck,
        // and its named failure is never masked by background recovery progress.
        #expect(model.unsealedRecheckTask == nil && !model.state.unsealedRecheckPending)
        #expect(model.captureTerminalNotice == model.state.status)
    }

    @Test @MainActor func foreignGenerationAndTakeCannotRequestReconnect() throws {
        let model = capturing()
        for terminal in [false, true] {
            model.handleRecorder(try event(terminal: terminal, generation: "old-generation"))
            model.handleRecorder(try event(terminal: terminal, takeID: "old-take"))
        }
        #expect(model.state.phase == .recording && model.state.canStop)
        #expect(model.state.errorCode == nil && !model.audioReconnectRequired)
    }

    @Test @MainActor func terminalFromReplacedStartCannotRetireActiveTransport() throws {
        let model = capturing()
        model.startOperationID = "new-start"
        model.handleRecorder(try event(terminal: true))
        #expect(model.state.phase == .recording)
        #expect(!model.audioReconnectRequired)
    }

    @Test @MainActor func sealedUnsavedDraftStillBlocksReconnectAndRemainsUnchanged() async {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .draftReview))
        model.state.take = .object(["decision": .string("draft"), "seal": .object(["audio": .object(["frames": .integer(480)])])])
        let take = model.state.take
        let selected = model.state.selection
        model.requireAudioReconnect(after: .deviceUnavailable)
        #expect(!model.canReconnectAudio)
        await model.reconnectAudio()
        #expect(model.state.take == take && model.state.selection == selected)
        #expect(model.state.phase == .draftReview && !model.canRecord)
    }
}
