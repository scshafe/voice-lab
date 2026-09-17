import Foundation
import RecordingContracts
@testable import RecordingCore
@testable import RecordingUI
import Testing

/// Pure-model coverage for Fine-tune model: every reply and event is a real wire shape (checked
/// against the committed response schema) delivered through scripted requests or the service
/// stream handler. No process is created and no audio is opened.
@Suite(.serialized)
struct RecordingFineTuneTests {
    static let job = "fine-tune-job-one"
    static let runID = "finetune-20260916t080000z"
    static let runRoot = "derived/finetune/\(runID)"
    static let supervisorFailure = "fine-tune supervision stopped; restart the app to recover the job"

    private static let repository = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()

    /// Scripted fine-tune requests: records each one, optionally holds it until released, then
    /// answers with the current scripted reply.
    @MainActor final class ScriptedFineTune {
        struct Sent: Equatable { let command: String; let operationID: String; let detail: String? }
        var sent: [Sent] = []
        var answered: [String: Int] = [:]
        var holding: Set<String> = []
        private var held: [String: [CheckedContinuation<Void, Never>]] = [:]
        var submitReply: (String) throws -> RecordingWireEnvelope = {
            try RecordingFineTuneTests.envelope(RecordingFineTuneTests.progress($0, "queued", runID: nil))
        }
        var cancelReply: (String) throws -> RecordingWireEnvelope = {
            try RecordingFineTuneTests.envelope(RecordingFineTuneTests.progress($0, "cancelled", runID: nil))
        }
        var statusReply: (String) throws -> RecordingWireEnvelope = {
            try RecordingFineTuneTests.envelope(RecordingFineTuneTests.status($0))
        }
        var commands: [String] { sent.map(\.command) }

        func release(_ command: String) {
            holding.remove(command)
            for waiter in held.removeValue(forKey: command) ?? [] { waiter.resume() }
        }

        var heldCount: Int { held.values.map(\.count).reduce(0, +) }

        /// The reply is the one scripted when the request is answered, after any hold.
        private func answer(_ command: String, _ operationID: String, _ detail: String?,
                            replyingFor replyOperation: String) async throws -> RecordingWireEnvelope {
            sent.append(Sent(command: command, operationID: operationID, detail: detail))
            if holding.contains(command) { await withCheckedContinuation { held[command, default: []].append($0) } }
            defer { answered[command, default: 0] += 1 }
            switch command {
            case "fine-tune": return try submitReply(replyOperation)
            case "cancel-fine-tune": return try cancelReply(replyOperation)
            default: return try statusReply(replyOperation)
            }
        }

        var requests: RecordingFineTuneRequests {
            RecordingFineTuneRequests(
                submit: { [self] operation, request in try await answer("fine-tune", operation, request, replyingFor: operation) },
                cancel: { [self] operation, job in try await answer("cancel-fine-tune", operation, job, replyingFor: job) },
                status: { [self] operation in try await answer("fine-tune-status", operation, nil, replyingFor: operation) })
        }
    }

    // MARK: wire shapes

    /// A service response that passed the Python-owned response schema, as the channel delivers it.
    static func envelope(_ data: RecordingJSON, error: RecordingJSON = .null,
                         requestID: String = "fixture-request") throws -> RecordingWireEnvelope {
        let body: RecordingJSON = .object(["data": data, "error": error, "meta": .object([
            "protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string(requestID),
            "process_generation": .string("synthetic-generation"), "state_revision": .integer(2),
            "event_sequence": .integer(3), "take_id": .null])])
        return try RecordingWireEnvelope.decode(body.transportData(), schema: Data(contentsOf:
            repository.appendingPathComponent("schemas/recording-response.schema.json")))
    }

    static func failure(_ code: String, _ message: String, retryable: Bool = false) -> RecordingJSON {
        .object(["code": .string(code), "message": .string(message), "retryable": .boolean(retryable)])
    }

    static func refusal(_ code: String, _ message: String, retryable: Bool = false) throws -> RecordingWireEnvelope {
        try envelope(.null, error: failure(code, message, retryable: retryable))
    }

    static func progress(_ operation: String = job, _ state: String, stage: String? = nil, epoch: Int64? = nil,
                         epochs: Int64? = nil, update: Int64? = nil, loss: Double? = nil, eta: Int64? = nil,
                         message: String? = nil, error: RecordingJSON? = nil,
                         runID: String? = RecordingFineTuneTests.runID) -> RecordingJSON {
        var event: [String: RecordingJSON] = ["event": .string("fine-tune-progress"), "operation_id": .string(operation),
                                              "state": .string(state)]
        if let stage { event["stage"] = .string(stage) }
        if let runID { event["run_id"] = .string(runID) }
        if let epoch { event["epoch"] = .integer(epoch) }
        if let epochs { event["epochs"] = .integer(epochs) }
        if let update { event["update"] = .integer(update) }
        if let loss { event["loss"] = .number(loss) }
        if let eta { event["eta_seconds"] = .integer(eta) }
        if let message { event["message"] = .string(message) }
        if let error { event["error"] = error }
        return .object(event)
    }

    /// Pass 30 of 120, 42 minutes left, loss 0.52.
    static func training(_ operation: String = job, epoch: Int64 = 30, update: Int64 = 270,
                         message: String? = nil) -> RecordingJSON {
        progress(operation, "running", stage: "training", epoch: epoch, epochs: 120, update: update,
                 loss: 0.52, eta: 2520, message: message)
    }

    static func summary(_ runID: String = RecordingFineTuneTests.runID, status: String = "finished") -> RecordingJSON {
        let root = "derived/finetune/\(runID)"
        var run: [String: RecordingJSON] = [
            "run_id": .string(runID), "run_root": .string(root), "status": .string(status),
            "started_at": .string("2026-09-16T08:00:00Z"), "epochs": .integer(120), "prompt_count": .integer(34),
            "retained_seconds": .number(181.25), "finetuned_sample_paths": .array([]), "base_sample_paths": .array([]),
        ]
        if status != "running" { run["finished_at"] = .string("2026-09-16T10:00:00Z") }
        if status == "failed" { run["error"] = .string("Synthetic training failure") }
        if status == "finished" {
            run["finetuned_sample_paths"] = .array([.string("\(root)/samples/finetuned/sample-01.wav")])
            run["base_sample_paths"] = .array([.string("\(root)/samples/base/sample-01.wav")])
        }
        return .object(run)
    }

    static func runEvent(_ operation: String = job, _ run: RecordingJSON = summary()) -> RecordingJSON {
        .object(["event": .string("fine-tune"), "operation_id": .string(operation), "run": run])
    }

    static func status(_ operation: String, configured: Bool = true, active: RecordingJSON? = nil,
                       latest: RecordingJSON? = nil) -> RecordingJSON {
        .object(["event": .string("fine-tune-status"), "operation_id": .string(operation),
                 "runtime_configured": .boolean(configured), "active": active ?? .null, "latest_run": latest ?? .null])
    }

    // MARK: helpers

    /// Lets the model's request tasks run until `condition` holds; records an issue otherwise.
    @MainActor private func waitUntil(_ condition: @MainActor () -> Bool,
                                      sourceLocation: SourceLocation = #_sourceLocation) async {
        for _ in 0..<5_000 {
            if condition() { return }
            await Task.yield()
        }
        #expect(condition(), "the scripted requests did not settle", sourceLocation: sourceLocation)
    }

    /// A workspace whose fine-tune requests are scripted; `configured` answers one status first.
    @MainActor private func workspace(_ service: ScriptedFineTune, phase: RecordingShellPhase = .ready,
                                      configured: Bool = true) async -> RecordingWorkspaceModel {
        let model = RecordingWorkspaceModel(fixture: .init(phase: phase))
        model.fineTuneRequests = service.requests
        if configured {
            await model.requestFineTuneStatus()
            service.sent.removeAll()
            service.answered.removeAll()
        }
        return model
    }

    /// A workspace showing `job` as restored by a status reply (running, pass 30 of 120).
    @MainActor private func runningWorkspace(_ service: ScriptedFineTune) async throws -> RecordingWorkspaceModel {
        service.statusReply = { try Self.envelope(Self.status($0, active: Self.training())) }
        let model = await workspace(service, configured: false)
        await model.requestFineTuneStatus()
        service.sent.removeAll()
        service.answered.removeAll()
        #expect(model.fineTune.operationID == Self.job && model.fineTune.phase == .running)
        return model
    }

    /// Starts a job and waits for its submit reply; returns the job's operation id.
    @MainActor private func start(_ model: RecordingWorkspaceModel, _ service: ScriptedFineTune) async throws -> String {
        let answered = service.answered["fine-tune", default: 0]
        model.startFineTune()
        await waitUntil { service.answered["fine-tune", default: 0] == answered + 1 }
        return try #require(service.sent.last { $0.command == "fine-tune" }?.operationID)
    }

    // MARK: client

    @Test func theClientsCommandsAreRequestSchemaShapesAndTheRequestIDNeverEqualsTheJob() async throws {
        let schema = try Data(contentsOf: Self.repository.appendingPathComponent("schemas/recording-service-request.schema.json"))
        func requireRequest(_ command: RecordingJSON) throws {
            let body: RecordingJSON = .object(["data": command, "error": .null, "meta": .object([
                "protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("request-fine-tune-one"),
                "process_generation": .string("synthetic-generation"), "state_revision": .integer(0),
                "event_sequence": .integer(0), "take_id": .null])])
            _ = try RecordingWireEnvelope.decode(body.transportData(), schema: schema)
        }
        let submit = RecordingServiceClient.fineTuneCommand(operationID: "fine-tune-one", epochs: nil, allowMissing: true)
        #expect(submit == .object(["command": .string("fine-tune"), "operation_id": .string("fine-tune-one"),
                                   "epochs": .null, "allow_missing": .boolean(true)]))
        try requireRequest(submit)
        try requireRequest(RecordingServiceClient.fineTuneCommand(operationID: "fine-tune-one", epochs: 3, allowMissing: false))
        #expect(throws: RecordingContractError.self) {
            try requireRequest(RecordingServiceClient.fineTuneCommand(operationID: "fine-tune-one", epochs: 0, allowMissing: true))
        }
        let cancel = RecordingServiceClient.cancelFineTuneCommand(operationID: "cancel-one", cancelledOperationID: "fine-tune-one")
        #expect(cancel.object?["cancelled_operation_id"] == .string("fine-tune-one"))
        try requireRequest(cancel)
        try requireRequest(RecordingServiceClient.fineTuneStatusCommand(operationID: "status-one"))
        #expect(RecordingServiceClient.fineTuneRequestTimeout == 30)

        // A request id equal to the job's own id is refused before anything is sent.
        let client = RecordingServiceClient(configuration: RecordingProcessConfiguration(
            executable: URL(fileURLWithPath: "/usr/bin/false"), arguments: [],
            schemaDirectory: Self.repository.appendingPathComponent("schemas"), processGeneration: "synthetic-generation",
            clientBuild: .null, expectedPeerBuild: .null, peer: .service))
        do {
            _ = try await client.fineTune(operationID: "fine-tune-one", requestID: "fine-tune-one", allowMissing: true)
            Issue.record("A request id equal to the operation id was accepted")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.INVALID_MESSAGE")
        }
        client.channel.close()
    }

    // MARK: status line

    @Test func theStatusLineNamesEveryPhaseAndStage() {
        var status = RecordingFineTuneStatus()
        #expect(status.statusLine == nil && !status.needsAttention)
        status.phase = .queued
        #expect(status.statusLine == "Fine-tuning · waiting to start")
        status.phase = .running
        #expect(status.statusLine == "Fine-tuning · starting")
        let stages: [(RecordingFineTuneStage, String)] = [
            (.selecting, "Fine-tuning · choosing the latest takes"),
            (.dataset, "Fine-tuning · preparing the recordings"),
            (.sampling, "Fine-tuning · writing sample sentences"),
        ]
        for (stage, line) in stages {
            status.stage = stage
            #expect(status.statusLine == line)
        }
        status.stage = .training
        status.epoch = 30; status.epochs = 120; status.etaSeconds = 2520; status.loss = 0.52
        #expect(status.statusLine == "Fine-tuning · pass 30 of 120 · 42 min left · loss 0.52")
        status.etaSeconds = 2521
        #expect(status.statusLine == "Fine-tuning · pass 30 of 120 · 43 min left · loss 0.52", "minutes round up")
        status.etaSeconds = 0; status.loss = 0.566
        #expect(status.statusLine == "Fine-tuning · pass 30 of 120 · 1 min left · loss 0.57")
        // Before the trainer prints, training reads pass 1 of N with nothing else.
        status.epoch = 1; status.etaSeconds = nil; status.loss = nil
        #expect(status.statusLine == "Fine-tuning · pass 1 of 120")
        status.message = "A notice from the service"
        #expect(status.statusLine == "A notice from the service")
        status.cancelRequested = true; status.message = nil
        #expect(status.statusLine == "Cancelling the fine-tune")
        status.cancelRequested = false
        status.phase = .finished; status.runID = Self.runID
        #expect(status.statusLine == "Model ready · \(Self.runID)")
        status.phase = .cancelled
        #expect(status.statusLine == "Fine-tune cancelled")
        status.phase = .failed; status.message = "the F5 trainer exited with status 1"
        #expect(status.statusLine == "the F5 trainer exited with status 1" && status.needsAttention)
        status.supervisorFailure = Self.supervisorFailure
        #expect(status.statusLine == Self.supervisorFailure)
    }

    @Test @MainActor func theStatusRegionShowsTheFineTuneLineUntilTheChannelCloses() async throws {
        let service = ScriptedFineTune()
        let model = try await runningWorkspace(service)
        #expect(model.fineTuneStatusLine == "Fine-tuning · pass 30 of 120 · 42 min left · loss 0.52")
        #expect(model.state.status != model.fineTuneStatusLine, "the recording status itself is untouched")
        model.processChannelFailed = true
        #expect(model.fineTuneStatusLine == nil && !model.fineTuneNeedsAttention)
        #expect(!model.canStartFineTune && model.fineTuneUnavailableReason == RecordingWorkspaceModel.processChannelFailedReason)
        #expect(!model.canCancelFineTune && model.fineTuneCancelReason == RecordingWorkspaceModel.processChannelFailedReason)
        // No request is made over a closed channel.
        await model.requestFineTuneStatus()
        #expect(service.sent.isEmpty)
    }

    // MARK: availability

    @Test @MainActor func availabilityNamesWhyFineTuneModelIsUnavailable() async throws {
        let unconfigured = RecordingWorkspaceModel(fixture: .init())
        #expect(!unconfigured.canStartFineTune && unconfigured.fineTuneUnavailableReason == "Recording service is unavailable")
        #expect(!unconfigured.canCancelFineTune && unconfigured.fineTuneCancelReason == "Recording service is unavailable")
        unconfigured.startFineTune()
        #expect(unconfigured.fineTune.phase == .idle)

        let service = ScriptedFineTune()
        let unknown = await workspace(service, configured: false)
        #expect(!unknown.canStartFineTune && unknown.fineTuneUnavailableReason == "Checking whether fine-tuning is available")
        unknown.startFineTune()
        #expect(unknown.fineTune.phase == .idle && service.sent.isEmpty)

        service.statusReply = { _ in try Self.refusal("RECORDING.GATE_NOT_READY", "fine-tuning is unavailable in this recording service") }
        await unknown.requestFineTuneStatus()
        #expect(unknown.fineTuneUnavailableReason == "fine-tuning is unavailable in this recording service")
        #expect(unknown.fineTuneStatusLine == nil)

        service.statusReply = { try Self.envelope(Self.status($0, configured: false)) }
        await unknown.requestFineTuneStatus()
        #expect(unknown.fineTune.runtimeConfigured == false && unknown.fineTune.availabilityError == nil)
        #expect(!unknown.canStartFineTune && unknown.fineTuneUnavailableReason == "Fine-tuning is not set up: this app has no F5 runtime to train with")

        service.statusReply = { try Self.envelope(Self.status($0)) }
        await unknown.requestFineTuneStatus()
        #expect(unknown.canStartFineTune)
        #expect(unknown.fineTuneUnavailableReason == "Train the voice model on the latest kept take of every recorded prompt. Recording stays available while it runs.")
        // A later failed status request does not forget what the service already said.
        service.statusReply = { _ in try Self.refusal("FINETUNE.SOURCE_CHANGED", "run.json under derived/finetune is not a run record") }
        await unknown.requestFineTuneStatus()
        #expect(unknown.canStartFineTune && unknown.fineTune.availabilityError == nil)

        for phase in [RecordingShellPhase.starting, .recording, .stopping] {
            let capturing = await workspace(ScriptedFineTune(), phase: phase)
            #expect(!capturing.canStartFineTune && capturing.fineTuneUnavailableReason == "Finish the current take before fine-tuning")
        }
        let saving = await workspace(ScriptedFineTune(), phase: .saving)
        #expect(!saving.canStartFineTune && saving.fineTuneUnavailableReason == "Wait for the current Save to finish before fine-tuning")
        for phase in [RecordingShellPhase.draftReview, .recoverableError] {
            #expect(await workspace(ScriptedFineTune(), phase: phase).canStartFineTune, "\(phase) does not block fine-tuning")
        }

        let queuedService = ScriptedFineTune()
        queuedService.holding = ["fine-tune"]
        let queued = await workspace(queuedService)
        queued.startFineTune()
        #expect(queued.fineTune.phase == .queued)
        #expect(!queued.canStartFineTune && queued.fineTuneUnavailableReason == "A fine-tune is already queued")
        #expect(queued.canCancelFineTune && queued.fineTuneCancelReason == "Stop the fine-tune. Your recordings are not affected.")
        queuedService.release("fine-tune")

        let running = try await runningWorkspace(ScriptedFineTune())
        #expect(!running.canStartFineTune && running.fineTuneUnavailableReason == "A fine-tune is already running")
        #expect(running.canCancelFineTune)
    }

    // MARK: start and events

    @Test @MainActor func startingSendsOneAllowMissingJobWithItsOwnRequestIDAndShowsItQueued() async throws {
        let service = ScriptedFineTune()
        let model = await workspace(service)
        let operation = try await start(model, service)
        #expect(service.commands == ["fine-tune"])
        let submit = try #require(service.sent.first)
        #expect(operation.hasPrefix("fine-tune-"))
        #expect(submit.detail == "request-" + operation && submit.detail != operation)
        #expect(model.fineTune.operationID == operation && model.fineTune.phase == .queued)
        #expect(model.fineTuneStatusLine == "Fine-tuning · waiting to start")
        #expect(model.fineTune.runtimeConfigured == true, "a new job keeps what the service said about the runtime")
        // Pressing again while it is queued sends nothing.
        model.startFineTune()
        #expect(service.commands == ["fine-tune"])
    }

    @Test @MainActor func jobEventsApplyInOrderOnceAndNeverMoveBackwards() async throws {
        let service = ScriptedFineTune()
        let model = await workspace(service)
        let job = try await start(model, service)
        func apply(_ event: RecordingJSON) throws {
            // Every reply and job event arrives twice: once to its caller, once on the stream.
            model.applyFineTuneEvent(event)
            let once = model.fineTune
            model.handleService(try Self.envelope(event, requestID: job))
            #expect(model.fineTune == once, "a duplicate changes nothing")
        }
        try apply(Self.progress(job, "running"))
        #expect(model.fineTuneStatusLine == "Fine-tuning · starting")
        try apply(Self.progress(job, "running", stage: "selecting"))
        try apply(Self.progress(job, "running", stage: "dataset"))
        #expect(model.fineTuneStatusLine == "Fine-tuning · preparing the recordings")
        try apply(Self.progress(job, "running", stage: "training", epoch: 1, epochs: 120))
        #expect(model.fineTuneStatusLine == "Fine-tuning · pass 1 of 120")
        try apply(Self.training(job))
        #expect(model.fineTuneStatusLine == "Fine-tuning · pass 30 of 120 · 42 min left · loss 0.52")
        let shown = model.fineTune
        // Stale copies: the submit reply, an earlier stage, an earlier pass or update, the start.
        for stale in [Self.progress(job, "queued", runID: nil), Self.progress(job, "running"),
                      Self.progress(job, "running", stage: "dataset"),
                      Self.progress(job, "running", stage: "training", epoch: 1, epochs: 120),
                      Self.training(job, epoch: 29, update: 300), Self.training(job, update: 269)] {
            try apply(stale)
            #expect(model.fineTune == shown, "\(stale) is older than what is shown")
        }
        try apply(Self.training(job, update: 271))
        #expect(model.fineTune.update == 271)
        try apply(Self.progress(job, "running", stage: "sampling"))
        #expect(model.fineTuneStatusLine == "Fine-tuning · writing sample sentences")
        try apply(Self.training(job, epoch: 120, update: 1080))
        #expect(model.fineTune.stage == .sampling)
        // A job ends with its run record, then its final state.
        try apply(Self.runEvent(job))
        #expect(model.fineTune.run?.runID == Self.runID && model.fineTune.latestRun?.runID == Self.runID)
        #expect(model.fineTune.latestRun?.finetunedSamplePaths == ["\(Self.runRoot)/samples/finetuned/sample-01.wav"])
        #expect(model.fineTune.phase == .running, "the run record alone does not end the job")
        try apply(Self.progress(job, "finished"))
        #expect(model.fineTune.phase == .finished && model.fineTuneStatusLine == "Model ready · \(Self.runID)")
        #expect(model.canStartFineTune && !model.canCancelFineTune)
        let finished = model.fineTune
        for late in [Self.progress(job, "running", stage: "sampling"),
                     Self.progress(job, "failed", error: Self.failure("FINETUNE.TRAINING_FAILED", "late")),
                     Self.progress(job, "cancelled")] {
            try apply(late)
            #expect(model.fineTune == finished, "a terminal state is final")
        }
    }

    @Test @MainActor func aJobEventThatOvertakesTheSubmitReplyIsKept() async throws {
        let service = ScriptedFineTune()
        service.holding = ["fine-tune"]
        let model = await workspace(service)
        model.startFineTune()
        await waitUntil { service.heldCount == 1 }
        let job = try #require(service.sent.first?.operationID)
        // The supervisor claimed the job and published before the submit reply was sent.
        model.handleService(try Self.envelope(Self.progress(job, "running"), requestID: job))
        #expect(model.fineTune.phase == .running)
        service.release("fine-tune")
        await waitUntil { service.answered["fine-tune"] == 1 }
        #expect(model.fineTune.phase == .running && model.fineTuneStatusLine == "Fine-tuning · starting")
        model.handleService(try Self.envelope(Self.progress(job, "queued", runID: nil), requestID: "request-" + job))
        #expect(model.fineTune.phase == .running)
    }

    @Test @MainActor func eventsOfAnotherJobAreIgnored() async throws {
        let service = ScriptedFineTune()
        let model = await workspace(service)
        // Nothing is shown yet, so no job event applies.
        model.handleService(try Self.envelope(Self.training(), requestID: Self.job))
        model.handleService(try Self.envelope(Self.runEvent(), requestID: Self.job))
        #expect(model.fineTune.phase == .idle && model.fineTune.operationID == nil && model.fineTune.latestRun == nil)
        let job = try await start(model, service)
        let shown = model.fineTune
        for foreign in [Self.training("fine-tune-other"), Self.progress("fine-tune-other", "finished"),
                        Self.progress("fine-tune-other", "cancelled"), Self.runEvent("fine-tune-other")] {
            model.handleService(try Self.envelope(foreign, requestID: "fine-tune-other"))
            #expect(model.fineTune == shown)
        }
        model.applyFineTuneEvent(.object(["event": .string("fine-tune-unknown"), "operation_id": .string(job)]))
        model.applyFineTuneEvent(.null)
        #expect(model.fineTune == shown)
    }

    @Test @MainActor func aFailedRunIsShownVerbatimAndNeverBecomesTheLatestModel() async throws {
        let service = ScriptedFineTune()
        service.statusReply = { try Self.envelope(Self.status($0, latest: Self.summary("finetune-earlier"))) }
        let model = await workspace(service, configured: false)
        await model.requestFineTuneStatus()
        #expect(model.fineTune.latestRun?.runID == "finetune-earlier")
        #expect(model.fineTune.phase == .idle && model.fineTuneStatusLine == nil, "an earlier run is not news")
        let job = try await start(model, service)
        #expect(model.fineTune.latestRun?.runID == "finetune-earlier")
        let message = "the F5 trainer exited with status 1; see derived/finetune/\(Self.runID)/train.log"
        model.handleService(try Self.envelope(Self.runEvent(job, Self.summary(status: "failed")), requestID: job))
        model.handleService(try Self.envelope(Self.progress(job, "failed", error: Self.failure("FINETUNE.TRAINING_FAILED", message)), requestID: job))
        #expect(model.fineTune.phase == .failed && model.fineTune.errorCode == "FINETUNE.TRAINING_FAILED")
        #expect(model.fineTuneStatusLine == message && model.fineTuneNeedsAttention)
        #expect(model.fineTune.run?.status == "failed" && model.fineTune.latestRun?.runID == "finetune-earlier")
        #expect(model.canStartFineTune, "a failed job can be tried again")
    }

    // MARK: refusals

    @Test @MainActor func aRefusedStartShowsTheServicesWordsAndAnActiveJobIsThenShown() async throws {
        let service = ScriptedFineTune()
        let model = await workspace(service)
        service.submitReply = { _ in try Self.refusal("FINETUNE.RUNTIME_UNAVAILABLE",
            "no F5 runtime python at /synthetic/runtime/bin/python; provision it with `voice-lab finetune provision-runtime`") }
        _ = try await start(model, service)
        #expect(model.fineTune.phase == .failed && model.fineTune.errorCode == "FINETUNE.RUNTIME_UNAVAILABLE")
        #expect(model.fineTuneStatusLine?.hasPrefix("no F5 runtime python at") == true)
        #expect(model.canStartFineTune)
        #expect(service.commands == ["fine-tune"], "only an active job is looked up")

        service.submitReply = { _ in try Self.refusal("FINETUNE.JOB_ACTIVE",
            "fine-tune fine-tune-other is already running; wait for it to finish or cancel it", retryable: true) }
        service.statusReply = { try Self.envelope(Self.status($0, active: Self.progress("fine-tune-other", "running", stage: "dataset"))) }
        service.holding = ["fine-tune-status"]
        model.startFineTune()
        await waitUntil { service.heldCount == 1 }
        #expect(model.fineTune.phase == .failed)
        #expect(model.fineTuneStatusLine == "fine-tune fine-tune-other is already running; wait for it to finish or cancel it")
        service.release("fine-tune-status")
        await model.fineTuneStatusTask?.value
        #expect(model.fineTune.operationID == "fine-tune-other" && model.fineTune.phase == .running)
        #expect(model.fineTuneStatusLine == "Fine-tuning · preparing the recordings")
        #expect(model.fineTune.errorCode == nil && model.canCancelFineTune)
        #expect(service.commands == ["fine-tune", "fine-tune", "fine-tune-status"])
    }

    @Test @MainActor func aSupervisorFailureIsShownAndStopsNewJobs() async throws {
        let service = ScriptedFineTune()
        let model = try await runningWorkspace(service)
        // Error-only envelopes of other requests are copies of replies their requests applied.
        model.handleService(try Self.envelope(.null, error: Self.failure("RECORDING.WRITE_FAILED", "recovery stopped", retryable: true),
                                              requestID: "recovery-supervisor"))
        model.handleService(try Self.refusal("FINETUNE.JOB_ACTIVE", "a copy of a refused reply"))
        #expect(model.fineTune.supervisorFailure == nil)
        #expect(model.fineTuneStatusLine == "Fine-tuning · pass 30 of 120 · 42 min left · loss 0.52")
        model.handleService(try Self.envelope(.null, error: Self.failure("RECORDING.WRITE_FAILED", Self.supervisorFailure, retryable: true),
                                              requestID: RecordingWorkspaceModel.fineTuneSupervisorRequestID))
        #expect(model.fineTuneStatusLine == Self.supervisorFailure && model.fineTuneNeedsAttention)
        #expect(!model.canStartFineTune && model.fineTuneUnavailableReason == Self.supervisorFailure)
        #expect(!model.canCancelFineTune && model.fineTuneCancelReason == Self.supervisorFailure)
        model.startFineTune()
        model.cancelFineTune()
        #expect(service.sent.isEmpty)
        // A later status reply does not hide the failure; only a restart recovers.
        await model.requestFineTuneStatus()
        #expect(model.fineTuneStatusLine == Self.supervisorFailure)
        #expect(model.state.phase == .ready && model.state.errorCode == nil, "recording is unaffected")
    }

    // MARK: restore

    @Test @MainActor func aRelaunchRestoresTheActiveJobAndTheLatestRunFromStatus() async throws {
        let service = ScriptedFineTune()
        service.statusReply = { try Self.envelope(Self.status($0, active: Self.training(), latest: Self.summary("finetune-earlier"))) }
        let model = await workspace(service, configured: false)
        // Before the status reply, this job's events belong to nobody the app shows.
        model.handleService(try Self.envelope(Self.progress(Self.job, "running", stage: "dataset"), requestID: Self.job))
        #expect(model.fineTune.phase == .idle)
        await model.requestFineTuneStatus()
        #expect(service.commands == ["fine-tune-status"])
        let status = try #require(service.sent.first)
        #expect(status.operationID.hasPrefix("fine-tune-status-"))
        #expect(model.fineTune.operationID == Self.job && model.fineTune.phase == .running && model.fineTune.stage == .training)
        #expect(model.fineTune.runtimeConfigured == true && model.fineTune.latestRun?.runID == "finetune-earlier")
        #expect(model.fineTuneStatusLine == "Fine-tuning · pass 30 of 120 · 42 min left · loss 0.52")
        #expect(!model.canStartFineTune && model.canCancelFineTune)
        // The status reply's stream copy changes nothing; the job's next events apply.
        let restored = model.fineTune
        model.handleService(try Self.envelope(Self.status(status.operationID, active: Self.training(), latest: Self.summary("finetune-earlier"))))
        #expect(model.fineTune == restored)
        model.handleService(try Self.envelope(Self.training(epoch: 31, update: 279), requestID: Self.job))
        #expect(model.fineTuneStatusLine == "Fine-tuning · pass 31 of 120 · 42 min left · loss 0.52")

        // A queued job restores as queued; a job being cancelled restores as cancelling.
        let queuedService = ScriptedFineTune()
        queuedService.statusReply = { try Self.envelope(Self.status($0, active: Self.progress(Self.job, "queued", runID: nil))) }
        let queued = await workspace(queuedService, configured: false)
        await queued.requestFineTuneStatus()
        #expect(queued.fineTune.phase == .queued && queued.fineTuneStatusLine == "Fine-tuning · waiting to start")
        let cancellingService = ScriptedFineTune()
        cancellingService.statusReply = { try Self.envelope(Self.status($0, active: Self.training(message: "Cancelling the fine-tune"))) }
        let cancelling = await workspace(cancellingService, configured: false)
        await cancelling.requestFineTuneStatus()
        #expect(cancelling.fineTune.cancelRequested && cancelling.fineTuneStatusLine == "Cancelling the fine-tune")
        #expect(!cancelling.canCancelFineTune && cancelling.fineTuneCancelReason == "The fine-tune is being cancelled")

        // No active job: nothing to show, the latest run is remembered, and a job can start.
        let idleService = ScriptedFineTune()
        idleService.statusReply = { try Self.envelope(Self.status($0, latest: Self.summary())) }
        let idle = await workspace(idleService, configured: false)
        await idle.requestFineTuneStatus()
        #expect(idle.fineTune.phase == .idle && idle.fineTuneStatusLine == nil && idle.canStartFineTune)
        #expect(idle.fineTune.latestRun == RecordingFineTuneRunSummary(Self.summary()))
    }

    @Test @MainActor func aJobThatEndsWhileItsStatusIsInFlightStaysEnded() async throws {
        let service = ScriptedFineTune()
        service.statusReply = { try Self.envelope(Self.status($0, active: Self.training())) }
        service.holding = ["fine-tune-status"]
        let model = await workspace(service, configured: false)
        let request = Task { await model.requestFineTuneStatus() }
        await waitUntil { service.heldCount == 1 }
        let statusOperation = try #require(service.sent.first?.operationID)
        // The stream delivers the reply's copy, then the job's end, before the request resumes.
        model.handleService(try Self.envelope(Self.status(statusOperation, active: Self.training())))
        model.handleService(try Self.envelope(Self.runEvent(), requestID: Self.job))
        model.handleService(try Self.envelope(Self.progress(Self.job, "finished"), requestID: Self.job))
        #expect(model.fineTune.phase == .finished && model.fineTune.latestRun?.runID == Self.runID)
        service.release("fine-tune-status")
        await request.value
        #expect(model.fineTune.phase == .finished && model.fineTuneStatusLine == "Model ready · \(Self.runID)")
    }

    @Test @MainActor func aStatusRequestedBeforeAStartNeverReplacesTheStartedJob() async throws {
        let service = ScriptedFineTune()
        let model = await workspace(service)
        service.holding = ["fine-tune-status"]
        let request = Task { await model.requestFineTuneStatus() }
        await waitUntil { service.heldCount == 1 }
        let job = try await start(model, service)
        // An old queue view names another job and an older latest run.
        model.fineTune.latestRun = RecordingFineTuneRunSummary(Self.summary("finetune-newer"))
        service.statusReply = { try Self.envelope(Self.status($0, active: Self.progress("fine-tune-elsewhere", "running"),
                                                           latest: Self.summary("finetune-older"))) }
        service.release("fine-tune-status")
        await request.value
        #expect(model.fineTune.operationID == job && model.fineTune.phase == .queued)
        #expect(model.fineTune.latestRun?.runID == "finetune-newer")
        // A status requested after the start describes it and moves it on.
        service.statusReply = { try Self.envelope(Self.status($0, active: Self.progress(job, "running", stage: "selecting"))) }
        await model.requestFineTuneStatus()
        #expect(model.fineTune.operationID == job && model.fineTuneStatusLine == "Fine-tuning · choosing the latest takes")
    }

    /// Scripted context bootstrap: the delivered catalog, an empty snapshot, then open-context.
    @MainActor final class ContextScript {
        var revision: Int64 = 0
        var refuseCatalog = false
        var refuseOpen = false
        var requests: RecordingContextRequests {
            RecordingContextRequests(
                catalog: { [self] operation in
                    if refuseCatalog {
                        return try RecordingCatalogDeliveryTests.errorReply("CORPUS.NO_ACTIVE_CATALOG", RecordingCatalogDeliveryTests.refusal)
                    }
                    return try RecordingCatalogDeliveryTests.reply(RecordingCatalogDeliveryTests.catalogEvent(
                        operation, catalog: RecordingCatalogDeliveryTests.pilotCatalog(),
                        reference: RecordingCatalogDeliveryTests.reference()))
                },
                snapshot: { try RecordingCatalogDeliveryTests.reply(RecordingCatalogDeliveryTests.historyPage(), schema: nil) },
                openContext: { [self] command in
                    if refuseOpen {
                        return try RecordingCatalogDeliveryTests.errorReply("RECORDING.WRITE_FAILED", "synthetic open-context failure")
                    }
                    defer { revision += 1 }
                    return try RecordingCatalogDeliveryTests.reply(RecordingCatalogDeliveryTests.contextEvent(
                        operation: command.object?["operation_id"]?.string ?? "", promptID: "reading-one",
                        digest: RecordingCatalogDeliveryTests.digest, revision: revision), schema: nil)
                })
        }
    }

    @Test @MainActor func statusIsRequestedRightAfterTheCatalogStepWhetherOrNotAContextOpens() async throws {
        let target = RecordingContextTarget(subjectID: "synthetic-subject",
            captureDevice: .object(["backend": .string("coreaudio"), "device_id": .string("synthetic-coreaudio-uid"),
                                    "display_name": .string("Synthetic UMIK-1")]),
            captureHost: "synthetic-host")

        // A refused catalog opens no context, yet fine-tuning is still asked about; a failed
        // status request is then the reason, word for word.
        let noCatalogScript = ContextScript()
        noCatalogScript.refuseCatalog = true
        let noCatalogService = ScriptedFineTune()
        noCatalogService.statusReply = { _ in try Self.refusal("RECORDING.GATE_NOT_READY", "fine-tuning is unavailable in this recording service") }
        let noCatalog = RecordingWorkspaceModel(bootstrap: noCatalogScript.requests, target: target)
        noCatalog.fineTuneRequests = noCatalogService.requests
        #expect(noCatalog.fineTuneUnavailableReason == "Checking whether fine-tuning is available")
        let openedWithoutCatalog = await noCatalog.prepareContext()
        #expect(!openedWithoutCatalog && noCatalog.state.errorCode == "CORPUS.NO_ACTIVE_CATALOG")
        await noCatalog.fineTuneStatusTask?.value
        #expect(noCatalogService.commands == ["fine-tune-status"])
        #expect(!noCatalog.canStartFineTune && noCatalog.fineTuneUnavailableReason == "fine-tuning is unavailable in this recording service")

        // A refused open-context: the status is known all the same.
        let script = ContextScript()
        script.refuseOpen = true
        let service = ScriptedFineTune()
        let model = RecordingWorkspaceModel(bootstrap: script.requests, target: target)
        model.fineTuneRequests = service.requests
        let opened = await model.prepareContext()
        #expect(!opened && model.state.phase == .recoverableError)
        await model.fineTuneStatusTask?.value
        #expect(service.commands == ["fine-tune-status"])
        #expect(model.fineTune.runtimeConfigured == true && model.canStartFineTune)

        // A reconnect asks again, even when its context is refused once more, and again when it opens.
        model.requireAudioReconnect(after: .deviceUnavailable)
        model.state.deviceReady = true // The fixture has no helper to prepare the device.
        #expect(model.canReconnectAudio)
        await model.reconnectAudio()
        await model.fineTuneStatusTask?.value
        #expect(model.audioReconnectRequired && service.commands == ["fine-tune-status", "fine-tune-status"])
        script.refuseOpen = false
        #expect(model.canReconnectAudio)
        await model.reconnectAudio()
        await model.fineTuneStatusTask?.value
        #expect(model.state.contextReady && !model.audioReconnectRequired)
        #expect(service.commands == ["fine-tune-status", "fine-tune-status", "fine-tune-status"])
    }

    // MARK: status region

    @MainActor final class ManualClock {
        var now = ContinuousClock.now
        func advance(_ duration: Duration) { now = now.advanced(by: duration) }
    }

    /// Every Record gate open, so a ready workspace carries no recording warning.
    @MainActor private func readyToRecord(_ model: RecordingWorkspaceModel) {
        model.state.contextReady = true; model.state.deviceReady = true; model.state.storageReady = true
        model.state.serviceReady = true; model.state.permission = .granted
        #expect(model.canRecord)
    }

    @MainActor private func setStatus(_ model: RecordingWorkspaceModel, _ status: String) {
        var updated = model.state
        updated.status = status
        model.publish(updated)
    }

    static let progressLine = "Fine-tuning · pass 30 of 120 · 42 min left · loss 0.52"

    @Test @MainActor func aRecordingStatusHoldsTheRegionForEightSecondsThenTheRunningJobShows() async throws {
        let model = try await runningWorkspace(ScriptedFineTune())
        let clock = ManualClock()
        model.statusClock = { clock.now }
        readyToRecord(model)
        // No recording status has changed yet: the running job shows.
        #expect(model.statusRegion == RecordingStatusRegion(line: Self.progressLine, source: .fineTune, needsAttention: false))
        setStatus(model, "Saved · verified")
        #expect(model.statusRegion == RecordingStatusRegion(line: "Saved · verified", source: .recording, needsAttention: false))
        clock.advance(.milliseconds(7_999))
        #expect(model.statusRegion.line == "Saved · verified")
        clock.advance(.milliseconds(1))
        #expect(model.statusRegion.line == Self.progressLine && model.statusRegion.source == .fineTune)
        // Publishing the same words again is not a change.
        model.publish(model.state)
        #expect(model.statusRegion.line == Self.progressLine)
        // A new recording status holds again; job progress does not shorten the hold.
        setStatus(model, "Draft · checking")
        clock.advance(.seconds(3))
        model.handleService(try Self.envelope(Self.training(epoch: 31, update: 279), requestID: Self.job))
        #expect(model.statusRegion.line == "Draft · checking")
        clock.advance(.seconds(5))
        #expect(model.statusRegion.line == "Fine-tuning · pass 31 of 120 · 42 min left · loss 0.52")
        // A legacy review status is recording feedback too.
        model.legacyReviewStatus = "Legacy take kept"
        #expect(model.statusRegion.line == "Legacy take kept")
        clock.advance(.seconds(8))
        #expect(model.statusRegion.source == .fineTune)
        // Recording speaks first, always, while a take is being recorded.
        model.state.phase = .recording
        #expect(model.statusRegion.line == model.state.status && model.statusRegion.source == .recording)
    }

    @Test @MainActor func recordingErrorsAndWarningsAlwaysWinOverAFineTune() async throws {
        let model = try await runningWorkspace(ScriptedFineTune())
        let clock = ManualClock()
        model.statusClock = { clock.now }
        readyToRecord(model)
        model.fail(RecordingContractError(code: "RECORDING.WRITE_FAILED", message: "Save could not be written"), phase: .draftReview)
        clock.advance(.seconds(600))
        #expect(model.statusRegion == RecordingStatusRegion(line: "Save could not be written", source: .recording, needsAttention: true))
        var cleared = model.state
        cleared.errorCode = nil; cleared.phase = .ready; cleared.status = "Ready to record"
        model.publish(cleared)
        clock.advance(.seconds(8))
        #expect(model.statusRegion.source == .fineTune)
        // Record unavailable (the region offers Check again) names its reason.
        model.state.deviceReady = false
        #expect(model.statusRegion.line == "Ready to record" && model.statusRegion.source == .recording)
        model.state.deviceReady = true
        #expect(model.statusRegion.source == .fineTune)
        // Audio to reconnect.
        model.requireAudioReconnect(after: .systemSleep)
        clock.advance(.seconds(600))
        #expect(model.statusRegion.source == .recording)
        model.audioReconnectRequired = false
        readyToRecord(model)
        #expect(model.statusRegion.source == .fineTune)
        // A capture that needs recovery.
        model.state.phase = .recoverableError
        #expect(model.statusRegion.source == .recording)
        model.state.phase = .ready
        // Recovery findings come before fine-tuning too.
        model.recoverySupervisorError = "Recovery inventory is unavailable"
        #expect(model.statusRegion == RecordingStatusRegion(line: "Recovery inventory is unavailable", source: .recovery, needsAttention: false))
    }

    @Test @MainActor func aFineTuneOutcomeShowsUntilTheRecordingStatusNextChanges() async throws {
        let service = ScriptedFineTune()
        let model = try await runningWorkspace(service)
        let clock = ManualClock()
        model.statusClock = { clock.now }
        readyToRecord(model)
        // A status changes, then the job finishes during the hold: recording first, then the outcome.
        setStatus(model, "Saved · verified")
        clock.advance(.seconds(2))
        model.handleService(try Self.envelope(Self.runEvent(), requestID: Self.job))
        model.handleService(try Self.envelope(Self.progress(Self.job, "finished"), requestID: Self.job))
        #expect(model.statusRegion.line == "Saved · verified")
        clock.advance(.seconds(6))
        #expect(model.statusRegion == RecordingStatusRegion(line: "Model ready · \(Self.runID)", source: .fineTune, needsAttention: false))
        clock.advance(.seconds(3_600))
        #expect(model.statusRegion.line == "Model ready · \(Self.runID)", "the outcome stays until something else happens")
        // The next recording status replaces it for good.
        setStatus(model, "Recording cancelled before input opened")
        clock.advance(.seconds(3_600))
        #expect(model.statusRegion.line == "Recording cancelled before input opened" && model.statusRegion.source == .recording)

        // The next job shows at once (starting it changes no recording status), then its outcome.
        let job = try await start(model, service)
        #expect(model.statusRegion.line == "Fine-tuning · waiting to start")
        model.handleService(try Self.envelope(Self.progress(job, "cancelled"), requestID: job))
        #expect(model.statusRegion == RecordingStatusRegion(line: "Fine-tune cancelled", source: .fineTune, needsAttention: false))
        setStatus(model, "Ready to record")
        clock.advance(.seconds(8))
        #expect(model.statusRegion.line == "Ready to record")

        // A failure warns while it shows, and not once recording feedback replaced it.
        let failed = try await start(model, service)
        let message = "the F5 trainer exited with status 1"
        model.handleService(try Self.envelope(Self.progress(failed, "failed", error: Self.failure("FINETUNE.TRAINING_FAILED", message)), requestID: failed))
        #expect(model.statusRegion == RecordingStatusRegion(line: message, source: .fineTune, needsAttention: true))
        #expect(model.statusRegion.spokenLine == "Warning: " + message)
        setStatus(model, "Ready to record · microphone checked")
        clock.advance(.seconds(8))
        #expect(model.statusRegion == RecordingStatusRegion(line: "Ready to record · microphone checked", source: .recording, needsAttention: false))

        // A refused start is an outcome too.
        service.submitReply = { _ in try Self.refusal("RECORDING.DISK_SPACE", "fine-tuning needs 8.0 GiB free on the library's disk; 2.1 GiB is free", retryable: true) }
        _ = try await start(model, service)
        #expect(model.statusRegion == RecordingStatusRegion(line: "fine-tuning needs 8.0 GiB free on the library's disk; 2.1 GiB is free",
                                                          source: .fineTune, needsAttention: true))
        // So is a stopped supervisor, which also keeps new jobs from starting.
        setStatus(model, "Saved · checking")
        clock.advance(.seconds(8))
        #expect(model.statusRegion.source == .recording)
        model.handleService(try Self.envelope(.null, error: Self.failure("RECORDING.WRITE_FAILED", Self.supervisorFailure, retryable: true),
                                              requestID: RecordingWorkspaceModel.fineTuneSupervisorRequestID))
        #expect(model.statusRegion == RecordingStatusRegion(line: Self.supervisorFailure, source: .fineTune, needsAttention: true))
        #expect(model.fineTuneSlot.help == Self.supervisorFailure)
    }

    @Test @MainActor func aRunRestoredFromAnEarlierSessionIsNotNews() async throws {
        let service = ScriptedFineTune()
        service.statusReply = { try Self.envelope(Self.status($0, latest: Self.summary())) }
        let model = await workspace(service, phase: .draftReview, configured: false)
        let clock = ManualClock()
        model.statusClock = { clock.now }
        await model.requestFineTuneStatus()
        clock.advance(.seconds(60))
        #expect(model.fineTune.latestRun?.runID == Self.runID)
        #expect(model.statusRegion == RecordingStatusRegion(line: model.state.status, source: .recording, needsAttention: false))
    }

    // MARK: footer slot, library coverage, dates

    @Test @MainActor func theFooterSlotIsOneControlThatStartsOrCancels() async throws {
        let unconfigured = RecordingWorkspaceModel(fixture: .init())
        #expect(unconfigured.fineTuneSlot == RecordingFineTuneSlot(title: "Fine-tune model", identifier: "transport.fine-tune",
                                                                   enabled: false, help: "Recording service is unavailable"))
        let service = ScriptedFineTune()
        let model = await workspace(service, phase: .draftReview)
        #expect(model.fineTuneSlot == RecordingFineTuneSlot(
            title: "Fine-tune model", identifier: "transport.fine-tune", enabled: true,
            help: "Train the voice model on the latest kept take of every recorded prompt. Recording stays available while it runs."))
        model.activateFineTuneSlot()
        #expect(model.fineTuneSlot == RecordingFineTuneSlot(title: "Cancel fine-tune", identifier: "transport.cancel-fine-tune",
                                                            enabled: true, help: "Stop the fine-tune. Your recordings are not affected."))
        await waitUntil { service.answered["fine-tune"] == 1 }
        let job = try #require(service.sent.first?.operationID)
        service.holding = ["cancel-fine-tune"]
        model.activateFineTuneSlot()
        #expect(model.fineTuneSlot == RecordingFineTuneSlot(title: "Cancel fine-tune", identifier: "transport.cancel-fine-tune",
                                                            enabled: false, help: "The fine-tune is being cancelled"))
        await waitUntil { service.heldCount == 1 }
        #expect(service.commands == ["fine-tune", "cancel-fine-tune"] && service.sent.last?.detail == job)
        service.release("cancel-fine-tune")
        await waitUntil { service.answered["cancel-fine-tune"] == 1 }
        #expect(model.fineTuneSlot.identifier == "transport.fine-tune" && model.fineTuneSlot.enabled)
        let recording = await workspace(ScriptedFineTune(), phase: .recording)
        #expect(recording.fineTuneSlot.help == "Finish the current take before fine-tuning" && !recording.fineTuneSlot.enabled)
    }

    @Test @MainActor func theLibraryHeaderCountsRecordedPromptsOverTheActiveCatalog() {
        let english = Locale(identifier: "en_US")
        #expect(RecordingLibraryCoverage(covered: 34, total: 1_886, locale: english)
                == RecordingLibraryCoverage(text: "34/1,886", spokenText: "34 of 1,886 prompts recorded"))
        #expect(RecordingLibraryCoverage(covered: nil, total: 1_886, locale: english)
                == RecordingLibraryCoverage(text: "—/1,886", spokenText: "1,886 prompts; recorded count is loading"))
        let model = RecordingWorkspaceModel(fixture: .init())
        #expect(model.libraryCoverage == RecordingLibraryCoverage(covered: nil, total: 1, locale: .current))
        model.state.historyCompleteness = "complete"
        model.state.coveredCount = 34; model.state.totalCount = 1_886
        #expect(model.libraryCoverage == RecordingLibraryCoverage(covered: 34, total: 1_886, locale: .current))
        for text in [model.libraryCoverage.text, model.libraryCoverage.spokenText] {
            #expect(!text.localizedCaseInsensitiveContains("room") && !text.localizedCaseInsensitiveContains("tone"))
        }
    }

    @Test func takeDatesNameNoSession() throws {
        let posix = Locale(identifier: "en_US_POSIX")
        let utc = try #require(TimeZone(secondsFromGMT: 0))
        #expect(RecordingTakePicker.recordedDate("2026-09-15T22:22:07.123456Z", precision: "take", locale: posix, timeZone: utc)
                == "Sep 15, 2026 at 10:22:07.123 PM")
        let imported = RecordingTakePicker.recordedDate("2026-09-15T22:22:07.123456Z", precision: "session", locale: posix, timeZone: utc)
        #expect(imported == "Sep 15, 2026 · exact time unknown")
        #expect(!imported.localizedCaseInsensitiveContains("session"))
        #expect(RecordingTakePicker.recordedDate(nil, precision: "unknown", locale: posix, timeZone: utc) == "Date unknown")
    }

    // MARK: cancel

    @Test @MainActor func cancellingARunningJobShowsCancellingUntilTheServiceReportsItCancelled() async throws {
        let service = ScriptedFineTune()
        let model = try await runningWorkspace(service)
        service.holding = ["cancel-fine-tune"]
        service.cancelReply = { try Self.envelope(Self.training($0, message: "Cancelling the fine-tune")) }
        model.cancelFineTune()
        // Said at once, before the reply.
        #expect(model.fineTune.cancelRequested && model.fineTuneStatusLine == "Cancelling the fine-tune")
        #expect(!model.canCancelFineTune && model.fineTuneCancelReason == "The fine-tune is being cancelled")
        #expect(!model.canStartFineTune && model.fineTuneUnavailableReason == "The fine-tune is being cancelled")
        await waitUntil { service.heldCount == 1 }
        model.cancelFineTune()
        #expect(service.sent.count == 1, "one cancel per job")
        let cancel = try #require(service.sent.first)
        #expect(cancel.command == "cancel-fine-tune" && cancel.detail == Self.job)
        #expect(cancel.operationID.hasPrefix("cancel-fine-tune-") && cancel.operationID != Self.job)
        service.release("cancel-fine-tune")
        await waitUntil { service.answered["cancel-fine-tune"] == 1 }
        #expect(model.fineTune.phase == .running && model.fineTuneStatusLine == "Cancelling the fine-tune")
        // Progress published before the group stopped carries no notice; the cancel still stands.
        model.handleService(try Self.envelope(Self.training(epoch: 31, update: 279), requestID: Self.job))
        #expect(model.fineTune.epoch == 31 && model.fineTuneStatusLine == "Cancelling the fine-tune")
        model.handleService(try Self.envelope(Self.progress(Self.job, "cancelled"), requestID: Self.job))
        #expect(model.fineTune.phase == .cancelled && !model.fineTune.cancelRequested && !model.fineTune.isActive)
        #expect(model.fineTuneStatusLine == "Fine-tune cancelled" && !model.fineTuneNeedsAttention)
        #expect(model.canStartFineTune && !model.canCancelFineTune)
        // The reply's stream copy arrives after the end and changes nothing.
        model.handleService(try Self.envelope(Self.training(message: "Cancelling the fine-tune")))
        #expect(model.fineTune.phase == .cancelled)
    }

    @Test @MainActor func cancellingAQueuedOrEndedJobAppliesTheReplyAsItsState() async throws {
        let service = ScriptedFineTune()
        let model = await workspace(service)
        let job = try await start(model, service)
        model.cancelFineTune()
        await waitUntil { service.answered["cancel-fine-tune"] == 1 }
        #expect(service.sent.last?.detail == job)
        #expect(model.fineTune.phase == .cancelled && model.fineTuneStatusLine == "Fine-tune cancelled")

        let ended = try await runningWorkspace(ScriptedFineTune())
        let endedService = ScriptedFineTune()
        ended.fineTuneRequests = endedService.requests
        endedService.cancelReply = { try Self.envelope(Self.progress($0, "finished")) }
        ended.cancelFineTune()
        await waitUntil { endedService.answered["cancel-fine-tune"] == 1 }
        #expect(ended.fineTune.phase == .finished && ended.fineTuneStatusLine == "Model ready · \(Self.runID)")
    }

    @Test @MainActor func aRefusedCancelIsExplainedAndCanBeTriedAgain() async throws {
        let service = ScriptedFineTune()
        let model = try await runningWorkspace(service)
        service.cancelReply = { try Self.refusal("RECORDING.INVALID_TRANSITION", "no fine-tune \($0) is queued or running") }
        model.cancelFineTune()
        await waitUntil { service.answered["cancel-fine-tune"] == 1 }
        #expect(!model.fineTune.cancelRequested && model.fineTune.phase == .running)
        #expect(model.fineTuneStatusLine == "no fine-tune \(Self.job) is queued or running")
        #expect(model.canCancelFineTune)
        // The job's next progress replaces the notice.
        model.handleService(try Self.envelope(Self.training(epoch: 31, update: 279), requestID: Self.job))
        #expect(model.fineTuneStatusLine == "Fine-tuning · pass 31 of 120 · 42 min left · loss 0.52")
    }

    // MARK: recording is unaffected

    @Test @MainActor func aFineTuneNeverChangesTheSelectionTheTakeOrSave() async throws {
        let service = ScriptedFineTune()
        let model = await workspace(service, phase: .draftReview)
        func snapshot() -> [String] {
            ["\(model.state.selection)", model.state.prompt?.id ?? "-", model.state.phase.rawValue, model.state.status,
             "\(model.state.take)", "\(model.canSave)", model.saveUnavailableReason, "\(model.state.canNavigate)",
             "\(model.canRecord)", model.recordUnavailableReason, "\(model.canSelectTake)", "\(model.state.errorCode ?? "-")",
             "\(model.transitions.count)", "\(model.reviewConfirmation == nil)"]
        }
        let before = snapshot()
        #expect(model.canStartFineTune, "a draft awaiting Save does not block fine-tuning")
        let job = try await start(model, service)
        #expect(snapshot() == before)
        for event in [Self.progress(job, "running"), Self.training(job), Self.runEvent(job), Self.progress(job, "finished")] {
            model.handleService(try Self.envelope(event, requestID: job))
            #expect(snapshot() == before)
        }
        let second = try await start(model, service)
        model.cancelFineTune()
        await waitUntil { service.answered["cancel-fine-tune"] == 1 }
        #expect(model.fineTune.phase == .cancelled)
        model.handleService(try Self.envelope(Self.progress(second, "failed", error: Self.failure("FINETUNE.INTERRUPTED", "the fine-tune was interrupted", retryable: true)),
                                              requestID: second))
        #expect(snapshot() == before)
    }
}
