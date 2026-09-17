import Foundation
import RecordingContracts
import RecordingCore

/// Where this app's fine-tune job stands. `idle`: no job was started or restored this session.
public enum RecordingFineTunePhase: String, Equatable, Sendable {
    case idle, queued, running, finished, failed, cancelled

    public var isActive: Bool { self == .queued || self == .running }
    public var isTerminal: Bool { self == .finished || self == .failed || self == .cancelled }
}

/// The step a running job is in, in the order the service runs them.
public enum RecordingFineTuneStage: String, Equatable, Sendable {
    case selecting, dataset, training, sampling

    var order: Int {
        switch self {
        case .selecting: 1
        case .dataset: 2
        case .training: 3
        case .sampling: 4
        }
    }
}

/// One training run as the service summarizes it; every path is relative to the asset root.
public struct RecordingFineTuneRunSummary: Equatable, Sendable {
    public let runID: String
    public let runRoot: String
    /// running, finished or failed.
    public let status: String
    public let startedAt: String
    public let finishedAt: String?
    public let epochs: Int64
    public let promptCount: Int64
    public let retainedSeconds: Double
    public let finetunedSamplePaths: [String]
    public let baseSamplePaths: [String]
    public let error: String?

    /// The service's `FineTuneRunSummary`, already validated on the wire; nil for any other shape.
    public init?(_ json: RecordingJSON) {
        guard let object = json.object, let runID = object["run_id"]?.string,
              let runRoot = object["run_root"]?.string, let status = object["status"]?.string,
              let startedAt = object["started_at"]?.string, let epochs = object["epochs"]?.integer,
              let promptCount = object["prompt_count"]?.integer,
              let retainedSeconds = object["retained_seconds"]?.number else { return nil }
        self.runID = runID
        self.runRoot = runRoot
        self.status = status
        self.startedAt = startedAt
        self.finishedAt = object["finished_at"]?.string
        self.epochs = epochs
        self.promptCount = promptCount
        self.retainedSeconds = retainedSeconds
        self.finetunedSamplePaths = (object["finetuned_sample_paths"]?.array ?? []).compactMap(\.string)
        self.baseSamplePaths = (object["base_sample_paths"]?.array ?? []).compactMap(\.string)
        self.error = object["error"]?.string
    }
}

/// Everything the app knows about fine-tuning. It lives beside the recording state, never in
/// it: no fine-tune event changes the selection, the take, Save or navigation.
public struct RecordingFineTuneStatus: Equatable, Sendable {
    /// The service's `active.message` while a cancel is pending.
    public static let cancellingMessage = "Cancelling the fine-tune"

    public var phase: RecordingFineTunePhase = .idle
    public var stage: RecordingFineTuneStage?
    public var operationID: String?
    public var runID: String?
    public var epoch: Int64?
    public var epochs: Int64?
    public var update: Int64?
    public var loss: Double?
    public var etaSeconds: Int64?
    /// The service's own words about this job: its cancel notice, a refused request, or its
    /// failure, verbatim.
    public var message: String?
    public var errorCode: String?
    /// A cancel of this job was requested and has not ended it yet.
    public var cancelRequested = false
    /// This job's run record, once the service published it.
    public var run: RecordingFineTuneRunSummary?
    /// The newest finished run the service knows (this session's or an earlier one).
    public var latestRun: RecordingFineTuneRunSummary?
    /// Whether the service has an F5 runtime; nil until it answered a status request.
    public var runtimeConfigured: Bool?
    /// Why availability is still unknown: the service refused or failed the status request.
    public var availabilityError: String?
    /// The service stopped supervising fine-tune jobs; its message (a restart recovers).
    public var supervisorFailure: String?

    public init() {}

    public var isActive: Bool { phase.isActive }
    public var needsAttention: Bool { supervisorFailure != nil || phase == .failed }

    /// One status-region line, or nil when there is nothing to say about fine-tuning.
    public var statusLine: String? {
        if let supervisorFailure { return supervisorFailure }
        switch phase {
        case .idle: return nil
        case .queued, .running:
            if cancelRequested { return message ?? Self.cancellingMessage }
            if let message { return message }
            guard phase == .running else { return "Fine-tuning · waiting to start" }
            switch stage {
            case nil: return "Fine-tuning · starting"
            case .selecting: return "Fine-tuning · choosing the latest takes"
            case .dataset: return "Fine-tuning · preparing the recordings"
            case .training: return trainingLine
            case .sampling: return "Fine-tuning · writing sample sentences"
            }
        case .finished: return "Model ready · " + (runID ?? run?.runID ?? "")
        case .failed: return message ?? "Fine-tune failed"
        case .cancelled: return "Fine-tune cancelled"
        }
    }

    /// `Fine-tuning · pass E of N · M min left · loss L`; parts the trainer has not reported
    /// yet are left out. Minutes round up, so the last minute reads "1 min left".
    private var trainingLine: String {
        var parts = ["Fine-tuning"]
        if let epoch, let epochs { parts.append("pass \(epoch) of \(epochs)") }
        if let etaSeconds { parts.append("\(max(1, etaSeconds / 60 + (etaSeconds % 60 == 0 ? 0 : 1))) min left") }
        if let loss { parts.append("loss " + String(format: "%.2f", loss)) }
        return parts.joined(separator: " · ")
    }

    /// Where a report places the job; a copy placing it earlier than the shown state is stale.
    fileprivate static func rank(_ phase: RecordingFineTunePhase, _ stage: RecordingFineTuneStage?) -> Int {
        switch phase {
        case .idle: 0
        case .queued: 1
        case .running: 2 + (stage?.order ?? 0)
        case .finished, .failed, .cancelled: 10
        }
    }

    /// Whether a report for this job is older than what is shown. Replies arrive twice (the
    /// reply and its stream copy), and a job event can overtake the submit reply, so states only
    /// move forward: queued, running through its stages, then one terminal state. Within
    /// training, the pass and then the update order reports; an unreported update is older.
    fileprivate func isStale(_ next: RecordingFineTunePhase, _ nextStage: RecordingFineTuneStage?,
                             epoch nextEpoch: Int64?, update nextUpdate: Int64?) -> Bool {
        let shown = Self.rank(phase, stage), incoming = Self.rank(next, nextStage)
        if incoming != shown { return incoming < shown }
        guard next == .running, nextStage == .training else { return false }
        if let nextEpoch, let epoch, nextEpoch != epoch { return nextEpoch < epoch }
        if let update { return (nextUpdate ?? -1) < update }
        return false
    }
}

/// The three service requests behind fine-tuning; fixtures script their replies.
struct RecordingFineTuneRequests {
    let submit: @MainActor (_ operationID: String, _ requestID: String) async throws -> RecordingWireEnvelope
    let cancel: @MainActor (_ operationID: String, _ cancelledOperationID: String) async throws -> RecordingWireEnvelope
    let status: @MainActor (_ operationID: String) async throws -> RecordingWireEnvelope
}

/// Fine-tuning from the workspace: one explicit job at a time, trained by the service on the
/// latest kept take of every recorded prompt. Nothing here reads or writes recording state.
@MainActor
extension RecordingWorkspaceModel {
    /// The request id of the service's error envelope when fine-tune supervision stops.
    public static let fineTuneSupervisorRequestID = "finetune-supervisor"
    static let fineTuneStatusRequestMemory = 16

    func fineTuneTransport() -> RecordingFineTuneRequests? {
        if let fineTuneRequests { return fineTuneRequests }
        guard let service else { return nil }
        return RecordingFineTuneRequests(
            submit: { try await service.fineTune(operationID: $0, requestID: $1, allowMissing: true) },
            cancel: { try await service.cancelFineTune(operationID: $0, cancelledOperationID: $1) },
            status: { try await service.fineTuneStatus(operationID: $0) })
    }

    /// The status-region line; nil once the service channel closed (the job went with it).
    public var fineTuneStatusLine: String? { processChannelFailed ? nil : fineTune.statusLine }
    public var fineTuneNeedsAttention: Bool { !processChannelFailed && fineTune.needsAttention }

    public var canStartFineTune: Bool { fineTuneStartBlocker == nil }
    /// Why Fine-tune model is unavailable, or what it does when it is available.
    public var fineTuneUnavailableReason: String {
        fineTuneStartBlocker ?? "Train the voice model on the latest take of every recorded prompt"
    }

    public var canCancelFineTune: Bool { fineTuneCancelBlocker == nil }
    public var fineTuneCancelReason: String {
        fineTuneCancelBlocker ?? "Stop the fine-tune; recordings are not affected"
    }

    private var fineTuneServiceBlocker: String? {
        if processChannelFailed { return Self.processChannelFailedReason }
        if fineTuneRequests == nil && service == nil { return "Recording service is unavailable" }
        return fineTune.supervisorFailure
    }

    private var fineTuneStartBlocker: String? {
        if let blocker = fineTuneServiceBlocker { return blocker }
        if fineTune.cancelRequested && fineTune.isActive { return "The fine-tune is being cancelled" }
        if fineTune.phase == .queued { return "A fine-tune is already queued" }
        if fineTune.phase == .running { return "A fine-tune is already running" }
        switch fineTune.runtimeConfigured {
        case nil: return fineTune.availabilityError ?? "Checking whether fine-tuning is available"
        case false?: return "Fine-tuning is not set up: this app has no F5 runtime"
        case true?: break
        }
        if state.showsStop { return "Finish the current take before fine-tuning" }
        if state.phase == .saving { return "Wait for the current Save to finish before fine-tuning" }
        return nil
    }

    private var fineTuneCancelBlocker: String? {
        if let blocker = fineTuneServiceBlocker { return blocker }
        guard fineTune.isActive, fineTune.operationID != nil else { return "No fine-tune is running" }
        return fineTune.cancelRequested ? "The fine-tune is being cancelled" : nil
    }

    /// Queue a job (always `allow_missing`: the catalog lists prompts nobody recorded yet). The
    /// status line says what started; the reply and the job's events move it on.
    public func startFineTune() {
        guard canStartFineTune else { return }
        let operation = newOperation("fine-tune")
        var started = RecordingFineTuneStatus()
        started.runtimeConfigured = fineTune.runtimeConfigured
        started.latestRun = fineTune.latestRun
        started.phase = .queued
        started.operationID = operation
        fineTuneGeneration &+= 1
        fineTune = started
        Task { [weak self] in
            guard let self, let requests = fineTuneTransport() else { return }
            do {
                // The job's own events carry its operation id as their request id.
                let response = try result(await requests.submit(operation, "request-" + operation))
                applyFineTuneEvent(response.data)
            } catch { refuseFineTune(operation, error) }
        }
    }

    /// A refused (or unanswered) submit replaces only the optimistic queued state it created.
    private func refuseFineTune(_ operation: String, _ error: Error) {
        guard fineTune.operationID == operation, fineTune.phase == .queued else { return }
        fineTuneGeneration &+= 1
        fineTune.phase = .failed
        fineTune.errorCode = (error as? RecordingContractError)?.code ?? "RECORDING.INVALID_MESSAGE"
        fineTune.message = errorMessage(error)
        fineTune.cancelRequested = false
        // Another job holds the queue: show that one instead.
        if fineTune.errorCode == "FINETUNE.JOB_ACTIVE" {
            fineTuneStatusTask = Task { [weak self] in await self?.requestFineTuneStatus() }
        }
    }

    /// Ask the service to stop the shown job. A queued job's reply is its cancelled state; a
    /// running one stays shown as cancelling until the service reports it cancelled.
    public func cancelFineTune() {
        guard canCancelFineTune, let job = fineTune.operationID else { return }
        fineTune.cancelRequested = true
        let operation = newOperation("cancel-fine-tune")
        Task { [weak self] in
            guard let self, let requests = fineTuneTransport() else { return }
            do {
                let response = try result(await requests.cancel(operation, job))
                applyFineTuneEvent(response.data)
            } catch {
                guard fineTune.operationID == job, fineTune.isActive else { return }
                fineTune.cancelRequested = false
                fineTune.message = errorMessage(error)
            }
        }
    }

    /// Request the service's fine-tune status without waiting for it: after a context opens
    /// (launch) and after an audio reconnect reopens one.
    func scheduleFineTuneStatus() {
        fineTuneStatusTask = Task { [weak self] in await self?.requestFineTuneStatus() }
    }

    /// Learn whether fine-tuning is available, restore a job still queued or running (after a
    /// relaunch) and the latest finished run.
    public func requestFineTuneStatus() async {
        guard !processChannelFailed, let requests = fineTuneTransport() else { return }
        let operation = newOperation("fine-tune-status")
        fineTuneStatusRequests.append((operationID: operation, generation: fineTuneGeneration))
        if fineTuneStatusRequests.count > Self.fineTuneStatusRequestMemory { fineTuneStatusRequests.removeFirst() }
        do {
            let response = try result(await requests.status(operation))
            applyFineTuneEvent(response.data)
        } catch {
            if fineTune.runtimeConfigured == nil { fineTune.availabilityError = errorMessage(error) }
        }
    }

    /// Apply a fine-tune event from a reply or the service stream; each arrives twice.
    /// Progress and run events apply only to the shown job's operation; a status reply may
    /// name another job, which the app then shows. Applying an event again changes nothing.
    public func applyFineTuneEvent(_ event: RecordingJSON) {
        guard let object = event.object else { return }
        switch object["event"]?.string {
        case "fine-tune-progress": applyFineTuneProgress(object)
        case "fine-tune": applyFineTuneRun(object)
        case "fine-tune-status": applyFineTuneStatus(object)
        default: break
        }
    }

    /// The service's error envelope when it stopped supervising jobs; no request awaits it.
    func applyFineTuneSupervisorError(_ error: RecordingJSON) {
        guard let message = error.object?["message"]?.string else { return }
        fineTune.supervisorFailure = message
    }

    private func applyFineTuneProgress(_ event: [String: RecordingJSON]) {
        guard let operation = event["operation_id"]?.string, operation == fineTune.operationID,
              !fineTune.phase.isTerminal,
              let next = event["state"]?.string.flatMap(RecordingFineTunePhase.init(rawValue:)), next != .idle else { return }
        let stage = event["stage"]?.string.flatMap(RecordingFineTuneStage.init(rawValue:))
        let epoch = event["epoch"]?.integer, update = event["update"]?.integer
        guard !fineTune.isStale(next, stage, epoch: epoch, update: update) else { return }
        var status = fineTune
        status.phase = next
        status.stage = stage
        status.runID = event["run_id"]?.string ?? status.runID
        status.epoch = epoch
        status.epochs = event["epochs"]?.integer
        status.update = update
        status.loss = event["loss"]?.number
        status.etaSeconds = event["eta_seconds"]?.integer
        let error = event["error"]?.object
        status.errorCode = error?["code"]?.string
        let message = event["message"]?.string
        if next.isTerminal {
            status.cancelRequested = false
            status.message = error?["message"]?.string ?? message
            fineTuneGeneration &+= 1
        } else {
            if message == RecordingFineTuneStatus.cancellingMessage { status.cancelRequested = true }
            // A notice stays until the job moves on, unless a cancel is still pending.
            if message != nil || !status.cancelRequested { status.message = message }
        }
        fineTune = status
    }

    private func applyFineTuneRun(_ event: [String: RecordingJSON]) {
        guard let operation = fineTune.operationID, event["operation_id"]?.string == operation,
              let run = event["run"].flatMap(RecordingFineTuneRunSummary.init) else { return }
        fineTune.run = run
        if run.status == "finished" { fineTune.latestRun = run }
    }

    private func applyFineTuneStatus(_ event: [String: RecordingJSON]) {
        guard let operation = event["operation_id"]?.string,
              case .boolean(let configured)? = event["runtime_configured"] else { return }
        // A reply requested before this app started, adopted or ended a job may describe an
        // older queue: it can still move the shown job forward, but it names no other job.
        let requested = fineTuneStatusRequests.last(where: { $0.operationID == operation })?.generation
        let current = requested.map { $0 == fineTuneGeneration } ?? true
        fineTune.runtimeConfigured = configured
        fineTune.availabilityError = nil
        if let latest = event["latest_run"].flatMap(RecordingFineTuneRunSummary.init),
           current || fineTune.latestRun == nil {
            fineTune.latestRun = latest
        }
        guard let active = event["active"]?.object, let job = active["operation_id"]?.string else { return }
        if job != fineTune.operationID {
            guard current else { return }
            var adopted = RecordingFineTuneStatus()
            adopted.runtimeConfigured = fineTune.runtimeConfigured
            adopted.latestRun = fineTune.latestRun
            adopted.supervisorFailure = fineTune.supervisorFailure
            adopted.operationID = job
            fineTuneGeneration &+= 1
            fineTune = adopted
        }
        applyFineTuneProgress(active)
    }
}
