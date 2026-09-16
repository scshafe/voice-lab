import Foundation
import RecordingContracts
@testable import RecordingHarness
@testable import RecordingUI
import Testing

/// Actual workspace projection/selection behavior with a deliberately held kept update.
/// The separate package-preparation integration exercises real Save and worker processes.
@Suite(.serialized)
struct RecordingPackagePreparationSynchronizationTests {
    private func prompt(_ takeID: String, roomTone: Bool = false) -> RecordingJSON {
        .object(["prompt_id": .string(takeID), "text": .string("Synthetic fixture"),
                 "category": .string(roomTone ? "room-tone" : "neutral")])
    }

    private func row(_ takeID: String, roomTone: Bool = false, decision: String = "kept") -> RecordingJSON {
        .object([
            "take_id": .string(takeID), "group_id": .string("group-package"),
            "prompt": prompt(takeID, roomTone: roomTone),
            "decision": .string(decision), "analysis": .string("verified"), "export": .string("unprepared"),
        ])
    }

    @MainActor private func project(_ model: RecordingWorkspaceModel, decision: String) {
        model.seedPackageFixture(groupID: "group-package", history: [
            row("take-room", roomTone: true), row("take-reading", decision: decision),
        ], catalog: .object(["purpose": .string("capture-pilot"), "prompts": .array([
            prompt("take-room", roomTone: true), prompt("take-reading"),
        ])]))
    }

    @Test @MainActor func readySaveWithHeldDraftProjectionCannotOpenPackageSelection() async throws {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        defer { model.close() }
        project(model, decision: "draft")
        let observer = ReviewObserver(model, barrierSeconds: 1)
        let takeIDs: Set<String> = ["take-room", "take-reading"]
        var entered = false
        var selectionOpened = false
        let opening = Task { @MainActor in
            entered = true
            try await RecordingPackageHarnessSynchronization.waitForEligibleTakes(
                takeIDs, model: model, observer: observer)
            // This is the actual sheet selection implementation, not a test copy of it.
            model.openPackageSelection()
            selectionOpened = true
        }
        for _ in 0..<20 {
            if entered { break }
            await Task.yield()
        }
        #expect(entered)
        #expect(model.state.phase == .ready)
        #expect(!selectionOpened && !model.packageSheetOpen)
        #expect(model.packageSelectedTakeIDs.isEmpty)
        // Release the exact missing post-Save projection; verification was already true.
        project(model, decision: "kept")
        try await opening.value
        #expect(selectionOpened && model.packageSheetOpen)
        #expect(model.packageSelectedTakeIDs == takeIDs)
        try RecordingPackageHarnessSynchronization.selectRoomToneOnly(takeIDs, model: model)
        #expect(model.packageSelectedTakeIDs == ["take-room"])
        try RecordingPackageHarnessSynchronization.selectAllRecorded(takeIDs, model: model)
        #expect(model.packageSelectedTakeIDs == takeIDs)
    }

    @Test @MainActor func staleInitialSelectionFailsBeforeTogglingAnUtteranceOn() throws {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        defer { model.close() }
        project(model, decision: "draft")
        model.openPackageSelection()
        #expect(model.packageSelectedTakeIDs == ["take-room"])
        #expect(throws: RecordingContractError.self) {
            try RecordingPackageHarnessSynchronization.selectRoomToneOnly(
                ["take-room", "take-reading"], model: model)
        }
        #expect(model.packageSelectedTakeIDs == ["take-room"])
    }

    @Test @MainActor func unexpectedPreparedResultFailsImmediatelyWithDurableStepAndSelection() async throws {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .ready))
        defer { model.close() }
        project(model, decision: "kept")
        model.openPackageSelection()
        model.seedPackagePreparation(operationID: "prepare-unexpected", groupID: "group-package")
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("package-sync-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false,
                                                attributes: [.posixPermissions: 0o700])
        let evidence = RecordingPackageHarnessEvidence(model: model, directory: directory)
        try evidence.checkpoint("await-blocked")
        let observer = ReviewObserver(model, barrierSeconds: 1)
        var entered = false
        let waiting = Task { @MainActor in
            entered = true
            do {
                try await RecordingPackageHarnessSynchronization.waitForTerminal(
                    .blocked, model: model, observer: observer)
                Issue.record("Unexpected prepared result satisfied the blocked scenario")
            } catch let error as RecordingContractError {
                #expect(error.code == "RECORDING.PACKAGE_HARNESS_UNEXPECTED_TERMINAL")
                try evidence.recordFailure(error)
            }
        }
        for _ in 0..<20 {
            if entered { break }
            await Task.yield()
        }
        #expect(entered)
        model.applyPackageEvent(.object([
            "event": .string("package"), "package": .object([
                "group": .object(["group_id": .string("group-package")]),
                "session_id": .string("synthetic-package"), "entries": .array([.object([:]), .object([:])]),
            ]),
        ]))
        try await waiting.value
        let report = try RecordingJSON.parse(Data(contentsOf: directory.appendingPathComponent("package-progress.json")))
        #expect(report.object?["completed"] == .boolean(false))
        #expect(report.object?["step"] == .string("await-blocked"))
        #expect(report.object?["failure"]?.string?.contains("UNEXPECTED_TERMINAL") == true)
        let final = try #require(report.object?["checkpoints"]?.array?.last?.object)
        #expect(final["package_phase"] == .string("prepared"))
        #expect(final["package_selected_take_ids"]?.array == [.string("take-reading"), .string("take-room")])
        print("package_sync_evidence=" + directory.path)
    }
}
