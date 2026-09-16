import AppKit
import Foundation
import RecordingContracts
import RecordingCore
@testable import RecordingUI
import Testing

@Suite(.serialized)
struct RecordingRecoveryInventoryTests {
    private func page(revision: Int64, generation: String = "scan-one", offset: Int = 0,
                      count: Int = 50, pending: Bool = true, complete: Bool = false) -> RecordingJSON {
        let entries = (offset..<(offset + count)).map { number -> RecordingJSON in
            let takeID = "take-\(number)"
            return .object(["take_id": .string(takeID), "observed_take_revision": .integer(1),
                "pending": .boolean(pending), "result": pending ? .null : .object([
                    "take_id": .string(takeID), "status": .string("empty"),
                    "recovered_seal": .boolean(false), "error": .null])])
        }
        return .object(["event": .string("recovery-inventory"), "page": .object([
            "scan_generation": .string(generation), "inventory_revision": .integer(revision),
            "scan_state": .string(complete ? "complete" : "scanning"),
            "enumeration_complete": .boolean(complete), "pending_count": .integer(pending ? 2_000 : 0),
            "finding_count": .integer(2_000), "recovered_seal_count": .integer(0),
            "covered_store_revision": complete ? .integer(12) : .null,
            "entries": .array(entries), "next_cursor": .string("next-page"), "error": .null])])
    }

    @Test @MainActor func thousandsOfFindingsKeepOnlyOneVisiblePageAndNeverSelectOrSave() {
        let model = RecordingWorkspaceModel(fixture: .init())
        let selected = model.state.selection
        for offset in stride(from: 0, to: 2_000, by: 50) {
            #expect(model.applyRecoveryInventory(page(revision: Int64(offset + 1), offset: offset)))
            #expect(model.recoveryRows.count == 50)
        }
        #expect(model.recoveryRows.keys.contains("take-1999"))
        #expect(!model.recoveryRows.keys.contains("take-0"))
        #expect(model.recoveryPendingCount == 2_000)
        #expect(model.recoveryScanState == "scanning")
        #expect(model.recoveryIncompleteRows.allSatisfy { $0.pending })
        #expect(model.state.selection == selected)
        #expect(!model.canSave)
    }

    @Test @MainActor func oldGenerationRevisionAndActionRepliesCannotReplaceInventory() {
        let model = RecordingWorkspaceModel(fixture: .init())
        #expect(model.applyRecoveryInventory(page(revision: 10)))
        #expect(!model.applyRecoveryInventory(page(revision: 9, offset: 50)))
        #expect(!model.applyRecoveryInventory(page(revision: 99, generation: "obsolete-scan", offset: 50)))
        model.seedRecoveryFixture(.object(["event": .string("recovery"), "page": .object([
            "entries": .array([.object(["take_id": .string("take-0"), "status": .string("kept"), "recovered_seal": .boolean(true)])])])]))
        #expect(model.recoveryRows["take-0"]?.pending == true)
        #expect(model.recoveredSealCount == 0)
        #expect(model.applyRecoveryInventory(page(revision: 11, pending: false, complete: true)))
        #expect(model.recoveryRows["take-0"]?.status == "empty")
    }

    @Test @MainActor func incompleteCoverageChoosesPlainSaveAndActiveCaptureCannotBeRechecked() {
        let model = RecordingWorkspaceModel(fixture: .init())
        for completeness in ["unknown", "building", "stale"] {
            model.state.historyCompleteness = completeness
            #expect(model.saveActionTitle == "Save")
            #expect(!model.saveAdvances)
        }
        model.state.historyCompleteness = "complete"
        #expect(model.saveActionTitle == "Save & Next")
        for phase in [RecordingShellPhase.starting, .recording, .stopping] {
            model.state.phase = phase
            #expect(!model.canRecheckRecoveryTake("synthetic-take"))
        }
    }

    @Test @MainActor func actualNotificationNamesStopOnceRequireReconnectAndDetachOnClose() {
        let center = NotificationCenter()
        let model = RecordingWorkspaceModel(fixture: .init(phase: .recording))
        model.observeSystemLifecycle(center: center)
        let selected = model.state.selection
        center.post(name: NSWorkspace.willSleepNotification, object: nil)
        center.post(name: NSWorkspace.willSleepNotification, object: nil)
        #expect(model.state.phase == .stopping)
        #expect(model.transitions.filter { $0.phase == "stopping" }.count == 1)
        center.post(name: NSWorkspace.didWakeNotification, object: nil)
        #expect(model.audioReconnectRequired)
        #expect(!model.systemSleeping)
        #expect(!model.canRecord)
        #expect(model.state.selection == selected)
        model.close()
        center.post(name: NSWorkspace.willSleepNotification, object: nil)
        #expect(!model.systemSleeping)
    }

    @Test @MainActor func sleepAndWakePreserveDraftAndDoNotEnableReconnectUntilResolved() {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .draftReview))
        let selected = model.state.selection
        model.handleSystemSleep(); model.handleSystemWake()
        #expect(model.state.phase == .draftReview)
        #expect(model.state.selection == selected)
        #expect(!model.canReconnectAudio)
        model.state.phase = .ready
        #expect(model.canReconnectAudio)
    }
}
