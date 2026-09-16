import Foundation
import RecordingContracts

/// Persistent Python service commands; metadata work never mediates native Stop.
public final class RecordingServiceClient: Sendable {
    public let channel: RecordingProcessChannel
    private let diagnosticSink: RecordingDiagnosticSink?

    public init(configuration: RecordingProcessConfiguration, diagnosticSink: RecordingDiagnosticSink? = nil) {
        precondition(configuration.peer == .service)
        channel = RecordingProcessChannel(configuration: configuration)
        self.diagnosticSink = diagnosticSink
    }

    public func connect() async throws { try await channel.start() }

    /// A request past its `timeout` closes the whole channel (the service is then gone).
    public func command(_ command: RecordingJSON, takeID: String?, requestID: String? = nil,
                        timeout: TimeInterval = 5,
                        diagnosticContext: RecordingDiagnosticContext? = nil) async throws -> RecordingWireEnvelope {
        let context = diagnosticSink == nil ? nil : (diagnosticContext ?? RecordingDiagnosticContext(traceID: UUID().uuidString.lowercased()))
            .request(requestID, operationID: command.object?["operation_id"]?.string, takeID: takeID)
        let span = RecordingDiagnosticSpan("service.request." + (command.object?["command"]?.string ?? "invalid"), context: context, sink: diagnosticSink)
        do {
            let response = try await channel.request(command, takeID: takeID, requestID: requestID, timeout: timeout)
            span.finish(response.error == .null ? .success : .failure, errorCode: response.error.object?["code"]?.string, requestID: response.meta.request_id)
            return response
        } catch { span.finish(error is CancellationError ? .cancelled : .failure, error: error); throw error }
    }

    /// The catalog this service process pinned from the corpus store: a CatalogEvent carrying
    /// the whole suite and its reference, or the service's refusal (for example
    /// CORPUS.NO_ACTIVE_CATALOG, whose message names `voice-lab catalog init`). The reply can
    /// approach the 1 MiB frame, so callers name a deadline longer than the default.
    public func catalog(operationID: String, timeout: TimeInterval) async throws -> RecordingWireEnvelope {
        try await command(.object(["command": .string("catalog"), "operation_id": .string(operationID)]),
                          takeID: nil, timeout: timeout)
    }

    /// Open (or reuse) a context. `command` names the catalog reference this service delivered
    /// (`RecordingContextTarget.openContextCommand`); the service refuses an embedded catalog.
    public func openContext(_ command: RecordingJSON, timeout: TimeInterval) async throws -> RecordingWireEnvelope {
        guard command.object?["command"] == .string("open-context") else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Open context requires an open-context command")
        }
        return try await self.command(command, takeID: nil, timeout: timeout)
    }

    public func selectPrompt(_ promptID: String, selection: RecordingJSON, operationID: String) async throws -> RecordingWireEnvelope {
        try await command(.object([
            "command": .string("navigate"), "operation_id": .string(operationID), "selection": selection,
            "mode": .string("selected-prompt"), "target_prompt_id": .string(promptID),
        ]), takeID: selection.object?["take_id"]?.string)
    }

    public func continueMissing(selection: RecordingJSON, operationID: String) async throws -> RecordingWireEnvelope {
        try await command(.object([
            "command": .string("navigate"), "operation_id": .string(operationID), "selection": selection,
            "mode": .string("continue-missing"), "target_prompt_id": .null,
        ]), takeID: selection.object?["take_id"]?.string)
    }

    public func save(selection: RecordingJSON, operationID: String, decisionRevision: Int64,
                     editRevision: Int64, frameRange: RecordingJSON, advance: Bool,
                     waveformReady: Bool) async throws -> RecordingWireEnvelope {
        guard waveformReady else {
            throw RecordingContractError(code: "RECORDING.INVALID_TRANSITION", message: "The matching stopped waveform must be ready before Save")
        }
        return try await command(.object([
            "command": .string("save"), "operation_id": .string(operationID), "selection": selection,
            "expected_decision_revision": .integer(decisionRevision), "expected_edit_revision": .integer(editRevision),
            "frame_range": frameRange, "advance": .boolean(advance), "waveform_ready": .boolean(true),
        ]), takeID: selection.object?["take_id"]?.string)
    }

    public func reserve(groupID: String, recorderGeneration: String, selection: RecordingJSON, operationID: String) async throws -> RecordingWireEnvelope {
        try await command(.object([
            "command": .string("reserve"), "operation_id": .string(operationID),
            "group_id": .string(groupID), "recorder_generation": .string(recorderGeneration),
            "selection": selection,
        ]), takeID: selection.object?["take_id"]?.string)
    }

    public func registerSeal(_ receipt: RecordingJSON, operationID: String, diagnosticContext: RecordingDiagnosticContext? = nil) async throws -> RecordingWireEnvelope {
        try await command(.object([
            "command": .string("register-seal"), "operation_id": .string(operationID), "receipt": receipt,
        ]), takeID: receipt.object?["take_id"]?.string, diagnosticContext: diagnosticContext)
    }

    public func review(takeID: String, selection: RecordingJSON, operationID: String, diagnosticContext: RecordingDiagnosticContext? = nil) async throws -> RecordingWireEnvelope {
        try await command(.object(["command": .string("review-take"), "operation_id": .string(operationID),
            "take_id": .string(takeID), "selection": selection]), takeID: takeID, diagnosticContext: diagnosticContext)
    }

    public func edit(takeID: String, selection: RecordingJSON, operationID: String,
                     sourceSHA256: String, editRevision: Int64, frameRange: RecordingJSON) async throws -> RecordingWireEnvelope {
        try await command(.object(["command": .string("edit"), "operation_id": .string(operationID),
            "take_id": .string(takeID), "selection": selection, "source_sha256": .string(sourceSHA256),
            "expected_edit_revision": .integer(editRevision), "frame_range": frameRange]), takeID: takeID)
    }

    public func decision(takeID: String, selection: RecordingJSON, operationID: String,
                         decisionRevision: Int64, action: String, reason: String?) async throws -> RecordingWireEnvelope {
        try await command(.object(["command": .string("decision"), "operation_id": .string(operationID),
            "take_id": .string(takeID), "selection": selection,
            "expected_decision_revision": .integer(decisionRevision), "action": .string(action),
            "reason": reason.map(RecordingJSON.string) ?? .null]), takeID: takeID)
    }

    public func retake(selection: RecordingJSON, operationID: String) async throws -> RecordingWireEnvelope {
        try await command(.object(["command": .string("retake"), "operation_id": .string(operationID),
            "selection": selection]), takeID: selection.object?["take_id"]?.string)
    }

    /// Submit-only: Python owns every package rule, eligibility check and durable write.
    /// The reply is the queue's first state; the rest arrive as unsolicited events.
    public func preparePackage(operationID: String, groupID: String, takeIDs: [String],
                               roomToneTakeIDs: [String]) async throws -> RecordingWireEnvelope {
        try await command(.object([
            "command": .string("prepare-package"), "operation_id": .string(operationID),
            "group_id": .string(groupID),
            "take_ids": .array(takeIDs.map(RecordingJSON.string)),
            "room_tone_take_ids": .array(roomToneTakeIDs.map(RecordingJSON.string)),
        ]), takeID: nil)
    }

    /// Bounded recovery. A single-take re-check names its take; a page sweep names none,
    /// and the two are mutually exclusive in the contract.
    public func recover(operationID: String, takeID: String? = nil,
                        afterTakeID: String? = nil, pageSize: Int = 50) async throws -> RecordingWireEnvelope {
        try await command(.object([
            "command": .string("recover"), "operation_id": .string(operationID),
            "take_id": takeID.map(RecordingJSON.string) ?? .null,
            "after_take_id": takeID == nil ? (afterTakeID.map(RecordingJSON.string) ?? .null) : .null,
            "page_size": .integer(Int64(min(50, max(1, pageSize)))),
        ]), takeID: takeID)
    }

    /// Read only the service's bounded recovery inventory; this never performs recovery.
    public func recoverySnapshot(cursor: String? = nil, pageSize: Int = 50) async throws -> RecordingWireEnvelope {
        try await command(.object([
            "command": .string("recovery-snapshot"),
            "cursor": cursor.map(RecordingJSON.string) ?? .null,
            "page_size": .integer(Int64(min(50, max(1, pageSize)))),
        ]), takeID: nil)
    }

    public func snapshot() async throws -> RecordingWireEnvelope { try await channel.requestSnapshot() }

    public func shutdown() async throws -> RecordingWireEnvelope {
        try await command(.object(["command": .string("shutdown")]), takeID: nil)
    }
}
