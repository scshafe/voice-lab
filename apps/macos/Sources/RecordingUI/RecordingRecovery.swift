import Foundation
import RecordingContracts
import RecordingCore

enum RecordingAudioReconnectCause: Equatable {
    case systemSleep, deviceUnavailable

    var contextDescription: String {
        switch self {
        case .systemSleep: "Audio reconnected after system sleep"
        case .deviceUnavailable: "Audio reconnected after microphone interruption"
        }
    }
}

/// Identity of one capture terminal; its recheck and presentation apply exactly once.
struct RecordingUnsealedTerminal: Equatable {
    let takeID: String
    let operationID: String
}

/// One bounded inventory row. Pending evidence is never rendered as a current diagnosis.
public struct RecordingRecoveryRow: Identifiable, Equatable, Sendable {
    public var id: String { takeID }
    public let takeID: String
    public let status: String
    public let errorCode: String?
    public let errorMessage: String?
    public let retryable: Bool
    public let recoveredSeal: Bool
    public let pending: Bool
    public let observedTakeRevision: Int64?

    public var isSealed: Bool { !pending && ["draft", "kept", "rejected", "removed"].contains(status) }
    public var isIncomplete: Bool { !isSealed }

    public init?(entry: RecordingJSON, pending: Bool = false, observedRevision: Int64? = nil) {
        guard let object = entry.object, let takeID = object["take_id"]?.string,
              let status = object["status"]?.string,
              ["draft", "kept", "rejected", "removed", "partial", "empty", "busy", "unavailable"].contains(status),
              (observedRevision ?? 0) >= 0 else { return nil }
        let error = object["error"]?.object
        let sealed = ["draft", "kept", "rejected", "removed"].contains(status)
        guard sealed == (error == nil) || (!sealed && status != "unavailable") else { return nil }
        guard status != "unavailable" || error != nil else { return nil }
        let recovered = object["recovered_seal"] == .boolean(true)
        guard !recovered || sealed else { return nil }
        self.takeID = takeID
        self.status = pending ? "pending" : status
        self.errorCode = pending ? nil : error?["code"]?.string
        self.errorMessage = pending ? "Awaiting recovery check · original material is retained" : error?["message"]?.string
        self.retryable = pending || error?["retryable"] == .boolean(true)
        self.recoveredSeal = !pending && recovered
        self.pending = pending
        self.observedTakeRevision = observedRevision
    }

    public init?(finding: RecordingJSON) {
        guard let object = finding.object, let takeID = object["take_id"]?.string,
              let pendingValue = object["pending"],
              pendingValue == .boolean(true) || pendingValue == .boolean(false) else { return nil }
        let pending = pendingValue == .boolean(true)
        let result = object["result"] ?? .null
        if result != .null {
            guard result.object?["take_id"] == .string(takeID) else { return nil }
            self.init(entry: result, pending: pending, observedRevision: object["observed_take_revision"]?.integer)
        } else {
            guard pending else { return nil }
            self.init(entry: .object(["take_id": .string(takeID), "status": .string("busy")]),
                      pending: true, observedRevision: object["observed_take_revision"]?.integer)
        }
    }
}

extension RecordingWorkspaceModel {
    public var recoveryIncompleteRows: [RecordingRecoveryRow] {
        recoveryRows.values.filter(\.isIncomplete).sorted { $0.takeID < $1.takeID }
    }

    public var recoveredSealCount: Int {
        recoveryScanGeneration == nil ? recoveryRows.values.filter(\.recoveredSeal).count : Int(recoveryRecoveredSealCount)
    }

    /// The service finding is unchanged. Only this session's own zero-frame Stop, which
    /// captured nothing, is named once in the status line instead of kept as an alarm.
    func isCancelledBeforeAudio(_ row: RecordingRecoveryRow) -> Bool {
        row.status == "partial" && row.errorCode == "RECORDING.CANCELLED" && cancelledBeforeAudioTakeIDs.contains(row.takeID)
    }

    public var recoveryAttentionRows: [RecordingRecoveryRow] {
        recoveryIncompleteRows.filter { !isCancelledBeforeAudio($0) }
    }

    private var recoveryAttentionCount: Int64 {
        recoveryScanGeneration == nil ? Int64(recoveryAttentionRows.count)
            : max(0, recoveryFindingCount - Int64(recoveryRows.values.filter(isCancelledBeforeAudio).count))
    }

    public var recoveryStatusLine: String? { recoveryProgressLine ?? recoveryFindingsLine }

    /// Recovery words that outrank a fine-tune line: a supervisor error or a scan in progress.
    var recoveryProgressLine: String? {
        if let error = recoverySupervisorError { return error }
        if recoveryScanGeneration != nil && recoveryScanState == "scanning" {
            return recoveryPendingCount == 0 ? "Checking preserved recordings…" : "Checking preserved recordings… · \(recoveryPendingCount) known captures awaiting check"
        }
        return nil
    }

    /// Standing findings (often from earlier sessions): one finding's own message, or their
    /// count. They stay reachable through Recovery… but never hide a fine-tune line.
    var recoveryFindingsLine: String? {
        let incomplete = recoveryAttentionRows
        if incomplete.count == 1 { return incomplete[0].errorMessage ?? "One recording needs attention" }
        let count = recoveryAttentionCount
        return count > 0 ? "\(count) recovery findings · originals are retained" : nil
    }

    /// Findings and failures stay reachable from the status line.
    public var canOpenRecovery: Bool {
        recoverySupervisorError != nil || !recoveryAttentionRows.isEmpty || recoveryAttentionCount > 0
    }

    public var recoveryEmptyMessage: String {
        if recoveryScanState == "error" { return "Recovery inventory is unavailable; preserved recordings have not been discarded." }
        if recoveryScanState != "complete" { return "Checking preserved recordings… More findings may appear." }
        return "No recordings need attention in this inventory."
    }

    public func canRecheckRecoveryTake(_ takeID: String) -> Bool {
        !(state.takeID == takeID && state.showsStop) && !recoveryInventoryLoading
    }

    /// Explicit recovery replies retain their original semantics. Inventory snapshots own
    /// the visible page; a reply cannot grow it into an unbounded collection.
    public func applyRecoveryEvent(_ event: RecordingJSON) {
        guard recoveryScanGeneration == nil else { return }
        guard event.object?["event"] == .string("recovery"),
              let entries = event.object?["page"]?.object?["entries"]?.array else { return }
        if let single = event.object?["take_id"]?.string {
            guard entries.count == 1, entries[0].object?["take_id"] == .string(single) else { return }
        }
        for entry in entries {
            guard let row = RecordingRecoveryRow(entry: entry) else { continue }
            if recoveryRows[row.takeID] != nil || recoveryRows.count < 50 {
                recoveryRows[row.takeID] = row
            }
        }
    }

    /// The service supplies one coherent page. Cached findings never select or authorize a take.
    @discardableResult
    public func applyRecoveryInventory(_ event: RecordingJSON) -> Bool {
        guard event.object?["event"] == .string("recovery-inventory"),
              let page = event.object?["page"]?.object,
              let generation = page["scan_generation"]?.string,
              let revision = page["inventory_revision"]?.integer, revision >= 0,
              let scanState = page["scan_state"]?.string,
              ["scanning", "complete", "error"].contains(scanState),
              let pending = page["pending_count"]?.integer, pending >= 0,
              let count = page["finding_count"]?.integer, count >= 0,
              let recovered = page["recovered_seal_count"]?.integer, recovered >= 0,
              let entries = page["entries"]?.array, entries.count <= 50,
              revision >= recoveryInventoryRevision, revision >= recoveryExpectedRevision,
              recoveryScanGeneration == nil || generation == recoveryScanGeneration else { return false }
        let rows = entries.compactMap { RecordingRecoveryRow(finding: $0) }
        guard rows.count == entries.count, Set(rows.map(\.takeID)).count == rows.count,
              count >= Int64(rows.count), pending >= Int64(rows.filter(\.pending).count),
              pending <= count, recovered >= Int64(rows.filter(\.recoveredSeal).count),
              recovered <= count - pending else { return false }
        let error = page["error"]?.object
        guard (scanState == "error") == (error != nil) else { return false }
        if scanState == "complete" {
            guard page["enumeration_complete"] == .boolean(true), pending == 0,
                  page["covered_store_revision"]?.integer != nil else { return false }
        } else if page["covered_store_revision"] != nil && page["covered_store_revision"] != .null { return false }
        recoveryScanGeneration = generation
        recoveryInventoryRevision = revision
        recoveryExpectedRevision = max(recoveryExpectedRevision, revision)
        recoveryScanState = scanState
        recoveryPendingCount = pending
        recoveryFindingCount = count
        recoveryRecoveredSealCount = recovered
        recoveryRows = Dictionary(uniqueKeysWithValues: rows.map { ($0.takeID, $0) })
        recoveryNextCursor = page["next_cursor"]?.string
        recoverySupervisorError = error?["message"]?.string
        return true
    }

    /// Notification delivery is an optimization; the snapshot is authoritative after handshake.
    /// A burst of notifications (one per processed page during a scan) costs one deferred load.
    public func invalidateRecoveryInventory(_ event: RecordingJSON) {
        guard event.object?["event"] == .string("recovery-inventory-changed"),
              let generation = event.object?["scan_generation"]?.string,
              let revision = event.object?["inventory_revision"]?.integer,
              recoveryScanGeneration == nil || generation == recoveryScanGeneration,
              revision > recoveryInventoryRevision else { return }
        recoveryExpectedRevision = max(recoveryExpectedRevision, revision)
        recoveryPageCursor = nil
        recoveryPreviousCursors.removeAll(keepingCapacity: true)
        scheduleRecoveryInventoryRefresh()
    }

    /// Notifications merge into one deferred refresh per window (see `backgroundRefreshWindow`).
    func scheduleRecoveryInventoryRefresh() {
        guard scheduledRecoveryRefresh == nil else { return }
        scheduledRecoveryRefresh = Task { [weak self] in
            let settled = await self?.settleBackgroundWindow() ?? false
            guard let self else { return }
            scheduledRecoveryRefresh = nil
            if settled { await refreshRecoveryInventory() }
        }
    }

    private func recoverySnapshotTransport() -> (@MainActor (String?) async throws -> RecordingWireEnvelope)? {
        if let recoverySnapshotRequest { return recoverySnapshotRequest }
        guard let service else { return nil }
        return { cursor in try await service.recoverySnapshot(cursor: cursor) }
    }

    public func refreshRecoveryInventory() async {
        guard !recoveryInventoryClosed else { return }
        if recoveryInventoryLoading { recoveryRefreshRequested = true; return }
        guard let request = recoverySnapshotTransport() else { return }
        recoveryInventoryLoading = true
        recoveryRequestGeneration &+= 1
        let requestGeneration = recoveryRequestGeneration
        let cursor = recoveryPageCursor
        defer {
            recoveryInventoryLoading = false
            if recoveryRefreshRequested && !recoveryInventoryClosed {
                recoveryRefreshRequested = false
                Task { [weak self] in await self?.refreshRecoveryInventory() }
            }
        }
        do {
            let response = try result(await request(cursor))
            guard requestGeneration == recoveryRequestGeneration else { return }
            if applyRecoveryInventory(response.data) { recoveryPagesLoaded += 1 }
            else if recoveryExpectedRevision > recoveryInventoryRevision { recoveryRefreshRequested = true }
        } catch {
            guard requestGeneration == recoveryRequestGeneration else { return }
            if let failure = error as? RecordingContractError,
               failure.code == "RECORDING.STALE_REVISION", cursor != nil {
                recoveryPageCursor = nil
                recoveryPreviousCursors.removeAll(keepingCapacity: true)
                recoveryRefreshRequested = true
            } else { recoverySupervisorError = errorMessage(error) }
        }
    }

    public func nextRecoveryPage() {
        guard !recoveryInventoryLoading, let next = recoveryNextCursor else { return }
        recoveryPreviousCursors.append(recoveryPageCursor)
        recoveryPageCursor = next
        Task { [weak self] in await self?.refreshRecoveryInventory() }
    }

    public func previousRecoveryPage() {
        guard !recoveryInventoryLoading, !recoveryPreviousCursors.isEmpty else { return }
        recoveryPageCursor = recoveryPreviousCursors.removeLast()
        Task { [weak self] in await self?.refreshRecoveryInventory() }
    }

    /// One explicit recheck; active startup/capture retains its reservation exclusion.
    /// Returns whether the request was sent; its reply or failure has then been applied.
    @discardableResult
    public func recoverTake(_ takeID: String) async -> Bool {
        guard canRecheckRecoveryTake(takeID) else { return false }
        let request: @MainActor (String, String) async throws -> RecordingWireEnvelope
        if let recoverRequest { request = recoverRequest }
        else if let service { request = { try await service.recover(operationID: $0, takeID: $1) } }
        else { return false }
        do {
            let response = try result(await request(newOperation("recover"), takeID))
            applyRecoveryEvent(response.data)
            recoveryPageCursor = nil
            recoveryPreviousCursors.removeAll(keepingCapacity: true)
            await refreshRecoveryInventory()
        } catch { recoverySupervisorError = errorMessage(error) }
        return true
    }

    public func openRecoverySheet() { recoverySheetOpen = true }
    public func dismissRecoverySheet() { recoverySheetOpen = false }
    public func seedRecoveryFixture(_ event: RecordingJSON) { applyRecoveryEvent(event) }

    public func handleSystemSleep() {
        guard !systemSleeping else { return }
        systemSleeping = true
        requireAudioReconnect(after: .systemSleep)
        if state.canStop { stop() }
        Task { [weak self] in
            guard let self else { return }
            do { try await review.playback.shutdown() } catch { review.report(error) }
        }
    }

    /// Invalidate readiness after a confirmed terminal device failure or actual sleep.
    /// Preserve the named failure, take, draft and trim until the operator resolves them.
    func requireAudioReconnect(after cause: RecordingAudioReconnectCause) {
        audioReconnectRequired = true; audioReconnectCause = cause
        var updated = state; updated.deviceReady = false; updated.contextReady = false
        updated.deviceReason = cause == .systemSleep ? "Reconnect audio after the Mac wakes" : "Reconnect the selected microphone, then choose Reconnect audio"
        updated.contextReason = "Reconnect audio to continue recording"
        publish(updated)
    }

    public func handleSystemWake() {
        guard systemSleeping else { return }
        systemSleeping = false
        var updated = state
        if updated.phase == .ready { updated.status = "Reconnect audio before recording" }
        publish(updated)
    }
}
