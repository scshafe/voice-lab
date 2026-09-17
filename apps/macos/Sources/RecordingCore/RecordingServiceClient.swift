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

    /// Fine-tune requests share the service's metadata queue with Save, and a request past its
    /// deadline closes the whole channel, so they wait as long as a context request.
    public static let fineTuneRequestTimeout: TimeInterval = 30

    /// Submit-only: Python owns the corpus selection, the runtime and every durable write. The
    /// reply is the job's state (a FineTuneProgressEvent); the rest arrive as unsolicited events
    /// whose request id is `operationID`, so this request names a different `requestID` and a
    /// job event can never be taken for its reply. `allowMissing` trains on the prompts that
    /// have a kept take; the app always sends true because its catalog lists unrecorded sets.
    /// Throws: RECORDING.INVALID_MESSAGE when `requestID` equals `operationID`; the service's
    /// refusal is the reply's error (FINETUNE.JOB_ACTIVE, FINETUNE.RUNTIME_UNAVAILABLE, ...).
    public func fineTune(operationID: String, requestID: String, epochs: Int? = nil,
                         allowMissing: Bool) async throws -> RecordingWireEnvelope {
        guard requestID != operationID else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE",
                                         message: "A fine-tune request id must differ from its job's operation id")
        }
        return try await command(Self.fineTuneCommand(operationID: operationID, epochs: epochs, allowMissing: allowMissing),
                                 takeID: nil, requestID: requestID, timeout: Self.fineTuneRequestTimeout)
    }

    /// Stop the durable job `cancelledOperationID` (the session's `cancel` cannot reach it).
    /// The reply is that job's state: cancelled when it was queued; running with the message
    /// "Cancelling the fine-tune" when it runs (a cancelled event follows once its processes
    /// are gone); its final state when it had already ended.
    public func cancelFineTune(operationID: String, cancelledOperationID: String) async throws -> RecordingWireEnvelope {
        try await command(Self.cancelFineTuneCommand(operationID: operationID, cancelledOperationID: cancelledOperationID),
                          takeID: nil, timeout: Self.fineTuneRequestTimeout)
    }

    /// Read-only: whether a runtime is configured, the queued or running job with its live
    /// progress, and the latest finished run (a FineTuneStatusEvent).
    public func fineTuneStatus(operationID: String) async throws -> RecordingWireEnvelope {
        try await command(Self.fineTuneStatusCommand(operationID: operationID), takeID: nil,
                          timeout: Self.fineTuneRequestTimeout)
    }

    static func fineTuneCommand(operationID: String, epochs: Int?, allowMissing: Bool) -> RecordingJSON {
        .object(["command": .string("fine-tune"), "operation_id": .string(operationID),
                 "epochs": epochs.map { RecordingJSON.integer(Int64($0)) } ?? .null,
                 "allow_missing": .boolean(allowMissing)])
    }

    static func cancelFineTuneCommand(operationID: String, cancelledOperationID: String) -> RecordingJSON {
        .object(["command": .string("cancel-fine-tune"), "operation_id": .string(operationID),
                 "cancelled_operation_id": .string(cancelledOperationID)])
    }

    static func fineTuneStatusCommand(operationID: String) -> RecordingJSON {
        .object(["command": .string("fine-tune-status"), "operation_id": .string(operationID)])
    }

    /// F5 flow steps the service uses when a Speak request names none.
    public static let speechDefaultSteps = 32

    /// Speak `text` with `model` ("latest", "base" or a finished run id), using the reference
    /// clip `referencePromptID` (nil: the service picks the run's first reference prompt).
    /// Submit-only, like `fineTune`: the reply is the request's queued SynthesisEvent, and its
    /// later states arrive as unsolicited events whose request id is `operationID`, so this
    /// request names a different `requestID`. It shares the fine-tune deadline, since it waits
    /// in the same metadata queue as Save.
    /// Throws: RECORDING.INVALID_MESSAGE when `requestID` equals `operationID` or the command
    /// breaks the request schema (text over 2,000 characters, a tab, steps outside 4...64);
    /// the service's refusal is the reply's error (FINETUNE.JOB_ACTIVE,
    /// FINETUNE.RUNTIME_UNAVAILABLE, FINETUNE.NO_FINISHED_RUN, ...).
    public func synthesize(operationID: String, requestID: String, text: String, model: String,
                           referencePromptID: String?, nfeSteps: Int = speechDefaultSteps) async throws -> RecordingWireEnvelope {
        guard requestID != operationID else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE",
                                         message: "A Speak request id must differ from its operation id")
        }
        return try await command(Self.synthesizeCommand(operationID: operationID, text: text, model: model,
                                                        referencePromptID: referencePromptID, nfeSteps: nfeSteps),
                                 takeID: nil, requestID: requestID, timeout: Self.fineTuneRequestTimeout)
    }

    /// Stop the Speak request `cancelledOperationID`. The reply is that request's state:
    /// cancelled when it was queued; running when it runs (a cancelled event follows once its
    /// worker is gone); its final state when it had already ended.
    /// Throws: the service's refusal is the reply's error (RECORDING.INVALID_TRANSITION when the
    /// service has no such request).
    public func cancelSynthesis(operationID: String, cancelledOperationID: String) async throws -> RecordingWireEnvelope {
        try await command(Self.cancelSynthesisCommand(operationID: operationID, cancelledOperationID: cancelledOperationID),
                          takeID: nil, timeout: Self.fineTuneRequestTimeout)
    }

    static func synthesizeCommand(operationID: String, text: String, model: String,
                                  referencePromptID: String?, nfeSteps: Int) -> RecordingJSON {
        .object(["command": .string("synthesize"), "operation_id": .string(operationID), "text": .string(text),
                 "model": .string(model), "reference_prompt_id": referencePromptID.map(RecordingJSON.string) ?? .null,
                 "nfe_steps": .integer(Int64(nfeSteps))])
    }

    static func cancelSynthesisCommand(operationID: String, cancelledOperationID: String) -> RecordingJSON {
        .object(["command": .string("cancel-synthesis"), "operation_id": .string(operationID),
                 "cancelled_operation_id": .string(cancelledOperationID)])
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
