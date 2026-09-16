import Foundation
import RecordingContracts
import RecordingCore

/// One indexed history row offered to the explicit Prepare for lab selection.
/// Eligibility is never recomputed here; the row reports what the index already knows.
public struct RecordingPackageCandidate: Identifiable, Equatable, Sendable {
    public var id: String { takeID }
    public let takeID: String
    public let promptID: String
    public let promptTitle: String
    public let category: String
    public let groupID: String?
    public let recordedAt: String?
    public let datePrecision: String
    public let decision: String
    public let analysis: String
    public let export: String

    public var isRoomTone: Bool { category == "room-tone" }
    /// What the index already decided. The service still owns the verdict.
    public var looksEligible: Bool { decision == "kept" && analysis == "verified" }
    public var ineligibleReason: String? {
        if decision != "kept" { return "Decision is \(decision), not kept" }
        if analysis != "verified" { return "Independent verification is \(analysis)" }
        return nil
    }

    public init?(row: RecordingJSON) {
        guard let object = row.object, let takeID = object["take_id"]?.string,
              let prompt = object["prompt"]?.object, let promptID = prompt["prompt_id"]?.string,
              let decision = object["decision"]?.string, let analysis = object["analysis"]?.string,
              let export = object["export"]?.string else { return nil }
        self.takeID = takeID
        self.promptID = promptID
        self.promptTitle = prompt["text"]?.string ?? promptID
        self.category = prompt["category"]?.string ?? "neutral"
        self.groupID = object["group_id"]?.string
        self.recordedAt = object["recorded_at"]?.string
        self.datePrecision = object["date_precision"]?.string ?? "unknown"
        self.decision = decision
        self.analysis = analysis
        self.export = export
    }

    /// Rows the selection may offer: the current group only, exact catalog prompts only, and
    /// never a procedure step, which has neither spoken text nor room tone to package.
    /// Legacy rows without a group are not offered; the service refused them anyway.
    public static func offered(history: [RecordingJSON], groupID: String?,
                               catalog: RecordingJSON) -> [RecordingPackageCandidate] {
        guard let groupID else { return [] }
        let prompts = (catalog.object?["prompts"]?.array ?? []).map(semanticPrompt)
        return history.compactMap { row -> RecordingPackageCandidate? in
            guard let candidate = RecordingPackageCandidate(row: row), candidate.groupID == groupID,
                  candidate.category != "procedure", let prompt = row.object?["prompt"],
                  prompts.contains(semanticPrompt(prompt)) else { return nil }
            return candidate
        }
    }

    /// Schema normalization inserts `null` and `[]` defaults into validated copies only, so
    /// both sides drop them before the exact comparison (docs/PROMPT_CATALOG_V2.md §2.7).
    private static func semanticPrompt(_ prompt: RecordingJSON) -> RecordingJSON {
        guard let object = prompt.object else { return prompt }
        return .object(object.filter { $0.value != .null && $0.value != .array([]) })
    }
}

public enum RecordingPackagePreparationPhase: String, Equatable, Sendable {
    case selecting, queued, running, prepared, blocked, failed
}

/// The whole explanation of a blocked preparation is the service's own message; the app
/// never paraphrases it into a per-take verdict and never turns it into a Save instruction.
public struct RecordingPackagePreparationStatus: Equatable, Sendable {
    public var phase: RecordingPackagePreparationPhase = .selecting
    public var operationID: String?
    public var groupID: String?
    public var errorCode: String?
    public var message: String?
    public var retryable = false
    public var sessionID: String?
    public var manifestSHA256: String?
    public var packagePath: String?
    public var selectedSuiteID: String?
    public var entryCount = 0
    public var attempts = 0

    public init() {}

    public var isActive: Bool { phase == .queued || phase == .running }

    public var statusLine: String? {
        switch phase {
        case .selecting: return nil
        case .queued: return "Lab package queued"
        case .running: return "Preparing lab package…"
        case .prepared: return "Lab package prepared · \(entryCount) recordings"
        case .blocked, .failed: return message
        }
    }
}

extension RecordingWorkspaceModel {
    /// The catalog every offered candidate must belong to exactly.
    var packageCatalog: RecordingJSON { configuration?.catalog ?? packageFixtureCatalog ?? .null }

    /// Physical-test builds keep Prepare for lab, but the sheet names what it produces.
    public var packageSheetTitle: String {
        packageCatalog.object?["purpose"]?.string == "physical-test"
            ? "Prepare diagnostic package" : "Prepare a lab package"
    }

    public var canPreparePackage: Bool {
        canSelectTake && state.groupID != nil && service != nil
            && !(packagePreparation?.isActive ?? false)
    }

    public var prepareUnavailableReason: String {
        if service == nil { return "Recording service is unavailable" }
        if state.groupID == nil { return "The recording context is still being prepared" }
        if state.showsStop { return "Finish the current take before preparing a package" }
        if state.registrationPending { return "The sealed take is being registered" }
        if state.phase == .saving { return "The current Save is awaiting its durable acknowledgment" }
        if state.navigationPending { return "Prompt selection is being saved" }
        if packagePreparation?.isActive ?? false { return "A lab package is already preparing" }
        return "Select saved, verified takes and explicit room tone for a lab package"
    }

    public var packageRoomToneSelection: [String] {
        packageCandidates.filter { packageSelectedTakeIDs.contains($0.takeID) && $0.isRoomTone }
            .map(\.takeID)
    }

    /// A shape check on the command about to be built, not a prerequisite verdict: the
    /// contract itself requires at least one declared room tone, so a package cannot even
    /// be asked for without one. Everything beyond that shape is the service's decision,
    /// including whether the declared room tone is actually compatible.
    public var canSubmitPackage: Bool {
        !(packagePreparation?.isActive ?? false)
            && packageCandidates.contains { packageSelectedTakeIDs.contains($0.takeID) && $0.isRoomTone }
    }

    /// Explained here rather than by a silently disabled control, and never by redirecting
    /// Save & Next or inserting room tone into the recording queue.
    public var packageSubmitUnavailableReason: String? {
        if packagePreparation?.isActive ?? false { return "A lab package is already preparing" }
        if canSubmitPackage { return nil }
        if !packageCandidates.contains(where: \.isRoomTone) {
            return "No room tone was recorded for this context. Record the room-tone prompt in this same setup, then prepare a package. Your saved takes stay saved."
        }
        return "Select the room tone recorded with these takes; a package must declare its own acoustic evidence."
    }

    /// Unsaved trim decisions stay explicit, exactly as they do for Retake and decisions.
    public func requestPreparePackage() {
        guard canPreparePackage else { return }
        if review.isDirty { reviewConfirmation = .discardTrim(.preparePackage); return }
        openPackageSelection()
    }

    /// The selection step itself, after the availability gate. Rows come from the indexed
    /// history projection; the initial selection follows what the index already decided.
    public func openPackageSelection() {
        packageCandidates = RecordingPackageCandidate.offered(
            history: state.history, groupID: state.groupID, catalog: packageCatalog)
        packageCandidatesTruncated = state.historyCursor != nil
        packageSelectedTakeIDs = Set(packageCandidates.filter(\.looksEligible).map(\.takeID))
        packageSheetOpen = true
    }

    public func togglePackageSelection(_ takeID: String) {
        if packageSelectedTakeIDs.contains(takeID) { packageSelectedTakeIDs.remove(takeID) }
        else { packageSelectedTakeIDs.insert(takeID) }
        // A changed selection is a different durable operation.
        if packagePreparation?.phase != .prepared { packageOperationID = nil }
    }

    public func dismissPackageSheet() { packageSheetOpen = false }

    public func submitPackagePreparation() {
        guard let service, let groupID = state.groupID, canSubmitPackage else { return }
        let selected = packageCandidates.filter { packageSelectedTakeIDs.contains($0.takeID) }
        let takeIDs = selected.map(\.takeID)
        let roomTones = selected.filter(\.isRoomTone).map(\.takeID)
        // One durable operation id per distinct selection, replayed verbatim on every retry.
        let operation = packageOperationID ?? newOperation("prepare-package")
        packageOperationID = operation
        var status = packagePreparation ?? RecordingPackagePreparationStatus()
        status.phase = .queued
        status.operationID = operation
        status.groupID = groupID
        status.errorCode = nil
        status.message = nil
        status.retryable = false
        packagePreparation = status
        Task { [weak self] in
            guard let self else { return }
            do {
                let response = try result(await service.preparePackage(
                    operationID: operation, groupID: groupID,
                    takeIDs: takeIDs, roomToneTakeIDs: roomTones))
                applyPackageEvent(response.data)
            } catch {
                guard packageOperationID == operation else { return }
                var failure = packagePreparation ?? RecordingPackagePreparationStatus()
                failure.phase = .blocked
                failure.errorCode = (error as? RecordingContractError)?.code
                failure.message = errorMessage(error)
                failure.retryable = true
                packagePreparation = failure
            }
        }
    }

    /// Synthetic-fixture entry point. Production history always arrives from the service's
    /// own indexed projection; nothing here decides eligibility.
    public func seedPackageFixture(groupID: String, history: [RecordingJSON], catalog: RecordingJSON) {
        packageFixtureCatalog = catalog
        var updated = state
        updated.groupID = groupID
        updated.history = history
        publish(updated)
    }

    /// Synthetic-fixture entry point for an in-flight preparation the app already submitted.
    public func seedPackagePreparation(operationID: String, groupID: String) {
        packageOperationID = operationID
        var status = RecordingPackagePreparationStatus()
        status.phase = .queued
        status.operationID = operationID
        status.groupID = groupID
        packagePreparation = status
    }

    public var packageOperationIdentity: String? { packageOperationID }

    /// Every package event arrives twice — once as the submit reply and once on the
    /// unsolicited stream — and queued/running repeat on each worker attempt.
    /// The service channel is the only production caller.
    public func applyPackageEvent(_ event: RecordingJSON) {
        switch event.object?["event"]?.string {
        case "package-preparation":
            guard let operation = event.object?["operation_id"]?.string,
                  operation == packageOperationID,
                  event.object?["group_id"]?.string == state.groupID,
                  var status = packagePreparation, status.phase != .prepared,
                  let next = event.object?["state"]?.string,
                  let phase = RecordingPackagePreparationPhase(rawValue: next) else { return }
            if phase == .running && status.phase != .running { status.attempts += 1 }
            status.phase = phase
            status.errorCode = event.object?["error"]?.object?["code"]?.string
            status.message = event.object?["error"]?.object?["message"]?.string
            status.retryable = event.object?["error"]?.object?["retryable"] == .boolean(true)
            packagePreparation = status
        case "package":
            guard let map = event.object?["package"]?.object,
                  map["group"]?.object?["group_id"]?.string == state.groupID,
                  packageOperationID != nil else { return }
            var status = packagePreparation ?? RecordingPackagePreparationStatus()
            status.phase = .prepared
            status.errorCode = nil
            status.message = nil
            status.retryable = false
            status.sessionID = map["session_id"]?.string
            status.manifestSHA256 = map["manifest_sha256"]?.string
            status.packagePath = map["package_path"]?.string
            status.selectedSuiteID = map["selected_suite"]?.object?["suite_id"]?.string
            status.entryCount = map["entries"]?.array?.count ?? 0
            packagePreparation = status
            // The library row, not this event, is the truth about each take's current state.
            Task { [weak self] in await self?.refreshHistory() }
        default: break
        }
    }
}
