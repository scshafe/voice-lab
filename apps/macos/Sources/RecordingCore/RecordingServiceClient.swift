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

    public func command(_ command: RecordingJSON, takeID: String?, requestID: String? = nil,
                        diagnosticContext: RecordingDiagnosticContext? = nil) async throws -> RecordingWireEnvelope {
        let context = diagnosticSink == nil ? nil : (diagnosticContext ?? RecordingDiagnosticContext(traceID: UUID().uuidString.lowercased()))
            .request(requestID, operationID: command.object?["operation_id"]?.string, takeID: takeID)
        let span = RecordingDiagnosticSpan("service.request." + (command.object?["command"]?.string ?? "invalid"), context: context, sink: diagnosticSink)
        do {
            let response = try await channel.request(command, takeID: takeID, requestID: requestID)
            span.finish(response.error == .null ? .success : .failure, errorCode: response.error.object?["code"]?.string, requestID: response.meta.request_id)
            return response
        } catch { span.finish(error is CancellationError ? .cancelled : .failure, error: error); throw error }
    }

    public func openContext(operationID: String, subjectID: String, catalog: RecordingJSON,
                            catalogSHA256: String, device: RecordingJSON, captureHost: String,
                            expectedSelectionRevision: Int64?, newSetup: Bool = false,
                            setupDescription: String = "", pilotApprovalPath: String? = nil,
                            pilotApprovalSHA256: String? = nil) async throws -> RecordingWireEnvelope {
        try await command(.object([
            "command": .string("open-context"), "operation_id": .string(operationID),
            "subject_id": .string(subjectID), "catalog": catalog, "catalog_sha256": .string(catalogSHA256),
            "capture_device": device, "capture_host": .string(captureHost),
            "expected_selection_revision": expectedSelectionRevision.map(RecordingJSON.integer) ?? .null,
            "new_setup": .boolean(newSetup), "setup_description": .string(setupDescription),
            "pilot_approval_path": pilotApprovalPath.map(RecordingJSON.string) ?? .null,
            "pilot_approval_sha256": pilotApprovalSHA256.map(RecordingJSON.string) ?? .null,
        ]), takeID: nil)
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
