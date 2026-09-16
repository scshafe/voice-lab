import Foundation
import RecordingContracts
import RecordingCore
@testable import RecordingUI
import Testing

/// P5: background notifications merge into one deferred refresh. A burst of analysis results or
/// inventory changes costs one request, and while a request is in flight at most one more is
/// queued behind it. Replies are scripted through the fixture seams; no process is created.
@Suite(.serialized)
struct RecordingRefreshCoalescingTests {
    @MainActor private final class Requests {
        var count = 0
        var latestRevision: Int64 = 0
        /// While set, every scripted reply waits for `release()`.
        var held = false
        private var waiters: [CheckedContinuation<Void, Never>] = []
        func hold() async { guard held else { return }; await withCheckedContinuation { waiters.append($0) } }
        func release() { let resumed = waiters; waiters.removeAll(); for waiter in resumed { waiter.resume() } }
    }

    private static let window = Duration.milliseconds(20)

    /// Fixture envelopes bypass only wire-shape validation; shared conformance suites own it.
    private static func envelope(_ data: RecordingJSON) throws -> RecordingWireEnvelope {
        let body: RecordingJSON = .object(["data": data, "error": .null,
            "meta": .object(["protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("fixture-request"),
                "process_generation": .string("synthetic-generation"), "state_revision": .integer(2), "event_sequence": .integer(3), "take_id": .null])])
        return try RecordingWireEnvelope.decode(body.transportData(), schema: Data(#"{"type":"object"}"#.utf8))
    }
    private static func historyPage(revision: Int64) -> RecordingJSON {
        .object(["event": .string("history"), "catalog_sha256": .string(String(repeating: "a", count: 64)), "index_revision": .integer(revision),
            "completeness": .string("complete"), "covered_prompt_count": .integer(0), "total_prompt_count": .integer(1),
            "takes": .array([]), "prompts": .array([]), "next_cursor": .null, "next_prompt_cursor": .null])
    }
    private static func analysisResult(takeID: String) -> RecordingJSON {
        .object(["event": .string("analysis"), "result": .object(["take_id": .string(takeID), "state": .string("verified"),
            "source_sha256": .string(String(repeating: "b", count: 64)), "edit_revision": .integer(0), "context_revision": .integer(0)])])
    }
    private static func inventoryPage(revision: Int64) -> RecordingJSON {
        .object(["event": .string("recovery-inventory"), "page": .object([
            "scan_generation": .string("scan-one"), "inventory_revision": .integer(revision), "scan_state": .string("scanning"),
            "enumeration_complete": .boolean(false), "pending_count": .integer(0), "finding_count": .integer(0), "recovered_seal_count": .integer(0),
            "covered_store_revision": .null, "entries": .array([]), "next_cursor": .null, "error": .null])])
    }
    private static func inventoryChanged(revision: Int64) -> RecordingJSON {
        .object(["event": .string("recovery-inventory-changed"), "scan_generation": .string("scan-one"), "inventory_revision": .integer(revision)])
    }
    /// A step that never settles is a failure, not a hang.
    @MainActor private static func settle(_ what: String, until predicate: @MainActor () -> Bool) async throws {
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
        while !predicate() {
            guard ContinuousClock.now < deadline else { throw RecordingContractError(code: "RECORDING.REVIEW_TIMEOUT", message: what) }
            try await Task.sleep(for: .milliseconds(2))
        }
    }
    /// Let several merge windows pass, so anything still scheduled would have fired.
    private static func idle() async throws { try await Task.sleep(for: window * 4) }

    @Test @MainActor func aBurstOfAnalysisResultsCostsOneDeferredHistoryRefreshWithOnePending() async throws {
        let model = RecordingWorkspaceModel(fixture: .init())
        model.backgroundRefreshWindow = Self.window
        let requests = Requests()
        defer { requests.release() }
        model.historyRequest = { _, _ in
            requests.count += 1
            let revision = Int64(requests.count)
            await requests.hold()
            return try Self.envelope(Self.historyPage(revision: revision))
        }
        for take in 1...5 { model.handleService(try Self.envelope(Self.analysisResult(takeID: "take-\(take)"))) }
        #expect(requests.count == 0)
        try await Self.settle("one deferred history refresh") { requests.count >= 1 }
        try await Self.idle()
        #expect(requests.count == 1 && model.state.historyRevision == 1 && !model.historyRefreshInFlight)

        // One in flight, one queued: results during a held request add exactly one follow-up.
        requests.held = true
        for take in 6...8 { model.handleService(try Self.envelope(Self.analysisResult(takeID: "take-\(take)"))) }
        try await Self.settle("second refresh issued") { requests.count == 2 }
        for take in 9...11 { model.handleService(try Self.envelope(Self.analysisResult(takeID: "take-\(take)"))) }
        try await Self.idle()
        #expect(requests.count == 2 && model.historyRefreshInFlight)
        requests.release()
        try await Self.settle("queued refresh issued") { requests.count == 3 }
        requests.release()
        try await Self.settle("queued refresh applied") { model.state.historyRevision == 3 && !model.historyRefreshInFlight }
        try await Self.idle()
        #expect(requests.count == 3)
    }

    @Test @MainActor func aBurstOfInventoryChangesCostsOneDeferredPageLoadWithOnePending() async throws {
        let model = RecordingWorkspaceModel(fixture: .init())
        model.backgroundRefreshWindow = Self.window
        let requests = Requests()
        defer { requests.release() }
        model.recoverySnapshotRequest = { cursor in
            requests.count += 1
            #expect(cursor == nil)
            await requests.hold()
            return try Self.envelope(Self.inventoryPage(revision: requests.latestRevision))
        }
        func notify(_ revisions: ClosedRange<Int64>) {
            for revision in revisions { requests.latestRevision = revision; model.invalidateRecoveryInventory(Self.inventoryChanged(revision: revision)) }
        }
        notify(1...5)
        #expect(requests.count == 0 && model.recoveryExpectedRevision == 5)
        try await Self.settle("one deferred page load") { requests.count >= 1 }
        try await Self.idle()
        #expect(requests.count == 1 && model.recoveryInventoryRevision == 5 && model.recoveryPagesLoaded == 1)

        requests.held = true
        notify(6...8)
        try await Self.settle("second page load issued") { requests.count == 2 }
        notify(9...11)
        try await Self.idle()
        #expect(requests.count == 2 && model.recoveryInventoryLoading)
        requests.release()
        try await Self.settle("queued page load issued") { requests.count == 3 }
        requests.release()
        try await Self.settle("queued page applied") { model.recoveryInventoryRevision == 11 && !model.recoveryInventoryLoading }
        try await Self.idle()
        #expect(requests.count == 3 && model.recoveryPagesLoaded == 3)
    }

    /// Closing the workspace drops a scheduled window instead of issuing a late request.
    @Test @MainActor func closeCancelsAScheduledRefresh() async throws {
        let model = RecordingWorkspaceModel(fixture: .init())
        model.backgroundRefreshWindow = Self.window
        let requests = Requests()
        model.historyRequest = { _, _ in requests.count += 1; return try Self.envelope(Self.historyPage(revision: 1)) }
        model.recoverySnapshotRequest = { _ in requests.count += 1; return try Self.envelope(Self.inventoryPage(revision: 1)) }
        model.handleService(try Self.envelope(Self.analysisResult(takeID: "take-1")))
        model.invalidateRecoveryInventory(Self.inventoryChanged(revision: 1))
        #expect(model.scheduledHistoryRefresh != nil && model.scheduledRecoveryRefresh != nil)
        model.close()
        try await Self.idle()
        #expect(requests.count == 0 && model.scheduledHistoryRefresh == nil && model.scheduledRecoveryRefresh == nil)
    }
}
