import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI
import Testing

/// Pure-model coverage for the explicit Prepare for lab flow. No process is created and no
/// audio is opened; the full chain is exercised by the package-preparation harness suite.
@Suite(.serialized)
struct RecordingPackageTests {
    private func prompt(_ id: String, category: String = "neutral") -> RecordingJSON {
        .object(["prompt_id": .string(id), "text": .string("Synthetic \(id)"),
                 "category": .string(category), "style": .string("Speak naturally"),
                 "expected_seconds": .number(1)])
    }

    private func catalog(_ prompts: [RecordingJSON], purpose: String = "capture-pilot") -> RecordingJSON {
        .object(["suite_id": .string("synthetic-package"), "purpose": .string(purpose), "prompts": .array(prompts)])
    }

    private var fixtureCatalog: RecordingJSON {
        catalog([prompt("room-tone", category: "room-tone"), prompt("reading-one"), prompt("reading-two")])
    }

    private func candidateRow(_ takeID: String, prompt promptID: String, category: String = "neutral",
                              decision: String = "kept", analysis: String = "verified",
                              export: String = "unprepared", group: String? = "group-a",
                              promptObject: RecordingJSON? = nil) -> RecordingJSON {
        .object([
            "take_id": .string(takeID), "group_id": group.map(RecordingJSON.string) ?? .null,
            "prompt": promptObject ?? prompt(promptID, category: category),
            "recorded_at": .string("2026-09-12T00:00:00.000000Z"), "date_precision": .string("take"),
            "decision": .string(decision), "analysis": .string(analysis), "export": .string(export),
        ])
    }

    private func preparationEvent(_ state: String, operation: String, group: String = "group-a",
                                  code: String? = nil, message: String? = nil,
                                  retryable: Bool = false) -> RecordingJSON {
        var event: [String: RecordingJSON] = [
            "event": .string("package-preparation"), "operation_id": .string(operation),
            "group_id": .string(group), "state": .string(state),
        ]
        if let code, let message {
            event["error"] = .object(["code": .string(code), "message": .string(message),
                                      "retryable": .boolean(retryable)])
        }
        return .object(event)
    }

    @MainActor private func preparedModel() -> RecordingWorkspaceModel {
        let model = RecordingWorkspaceModel(fixture: .init())
        model.seedPackageFixture(
            groupID: "group-a",
            history: [
                candidateRow("take-tone", prompt: "room-tone", category: "room-tone"),
                candidateRow("take-one", prompt: "reading-one"),
                candidateRow("take-two", prompt: "reading-two", analysis: "pending"),
            ], catalog: fixtureCatalog)
        return model
    }

    @Test @MainActor func eligibilityIsReportedByTheIndexAndNeverRecomputedLocally() {
        let model = preparedModel()
        // Without a live service the explicit action refuses and explains itself.
        #expect(!model.canPreparePackage)
        #expect(model.prepareUnavailableReason == "Recording service is unavailable")
        model.requestPreparePackage()
        #expect(!model.packageSheetOpen)
        model.openPackageSelection()
        #expect(model.packageSheetOpen)
        #expect(model.packageCandidates.count == 3)
        // The unverified row is offered and explained, not hidden or silently dropped.
        let pending = model.packageCandidates.first { $0.takeID == "take-two" }
        #expect(pending?.ineligibleReason == "Independent verification is pending")
        #expect(!model.packageSelectedTakeIDs.contains("take-two"))
        #expect(model.packageSelectedTakeIDs == ["take-tone", "take-one"])
        #expect(model.canSubmitPackage)
    }

    @Test @MainActor func candidatesAreCurrentGroupExactCatalogPromptsAndNeverProcedureSteps() {
        let step = RecordingJSON.object([
            "prompt_id": .string("p10-unplug"), "instructions": .string("Unplug the synthetic device."),
            "category": .string("procedure"), "style": .string("Follow the steps"),
            "labels": .array([.string("test")]), "expected_seconds": .number(1)])
        let tone = RecordingJSON.object([
            "prompt_id": .string("p04-room-tone"), "instructions": .string("Remain still."),
            "category": .string("room-tone"), "style": .string("Silence"),
            "labels": .array([.string("test")]), "expected_seconds": .number(1)])
        let physical = catalog([tone, step], purpose: "physical-test")
        // Normalized history rows carry inserted null/[] defaults the raw catalog omits.
        var normalizedTone = tone.object!
        normalizedTone["text"] = .null
        let model = RecordingWorkspaceModel(fixture: .init())
        model.seedPackageFixture(groupID: "group-a", history: [
            candidateRow("take-tone", prompt: "p04-room-tone", promptObject: .object(normalizedTone)),
            candidateRow("take-step", prompt: "p10-unplug", promptObject: step),
            candidateRow("take-other-group", prompt: "p04-room-tone", group: "group-b", promptObject: tone),
            candidateRow("take-legacy", prompt: "p04-room-tone", group: nil, promptObject: tone),
            candidateRow("take-changed", prompt: "p04-room-tone", promptObject: .object(
                tone.object!.merging(["instructions": .string("Remain still and silent.")]) { $1 })),
        ], catalog: physical)
        model.openPackageSelection()
        #expect(model.packageCandidates.map(\.takeID) == ["take-tone"])
        #expect(model.packageSheetTitle == "Prepare diagnostic package")
        #expect(preparedModel().packageSheetTitle == "Prepare a lab package")
        // Without a group there is nothing the service would accept.
        #expect(RecordingPackageCandidate.offered(history: [candidateRow("take-tone", prompt: "room-tone", category: "room-tone")],
                                                  groupID: nil, catalog: fixtureCatalog).isEmpty)
    }

    @Test @MainActor func aContextWithoutRoomToneExplainsItselfInsteadOfSubmitting() {
        let model = RecordingWorkspaceModel(fixture: .init())
        model.seedPackageFixture(groupID: "group-a", history: [candidateRow("take-one", prompt: "reading-one")],
                                 catalog: fixtureCatalog)
        model.openPackageSelection()
        #expect(!model.canSubmitPackage)
        #expect(model.packageSubmitUnavailableReason?.contains("No room tone") == true)
        // The explanation never becomes an instruction to change Save.
        #expect(model.packageSubmitUnavailableReason?.contains("Save & Next") == false)
    }

    @Test @MainActor func blockedPreparationLeavesSaveNavigationAndRecordUntouched() {
        let model = preparedModel()
        model.openPackageSelection()
        let save = model.canSave
        let reason = model.saveUnavailableReason
        let prompt = model.state.prompt?.id
        let selection = model.state.selection
        model.seedPackagePreparation(operationID: "prepare-1", groupID: "group-a")
        model.applyPackageEvent(preparationEvent("blocked", operation: "prepare-1",
                                                 code: "RECORDING.GATE_NOT_READY",
                                                 message: "select explicit compatible room-tone takes for this package"))
        #expect(model.packagePreparation?.phase == .blocked)
        #expect(model.packagePreparation?.message == "select explicit compatible room-tone takes for this package")
        #expect(model.canSave == save && model.saveUnavailableReason == reason)
        #expect(model.state.prompt?.id == prompt && model.state.selection == selection)
        #expect(model.state.phase != .recoverableError)
    }

    @Test @MainActor func everyPackageEventIsIdempotentAndTerminalStatesWin() {
        let model = preparedModel()
        model.openPackageSelection()
        model.seedPackagePreparation(operationID: "prepare-1", groupID: "group-a")
        // The submit reply and the unsolicited stream both deliver the same events.
        for _ in 0..<2 { model.applyPackageEvent(preparationEvent("queued", operation: "prepare-1")) }
        for _ in 0..<2 { model.applyPackageEvent(preparationEvent("running", operation: "prepare-1")) }
        #expect(model.packagePreparation?.attempts == 1)
        model.applyPackageEvent(.object([
            "event": .string("package"),
            "package": .object([
                "session_id": .string("native-root-abc"), "manifest_sha256": .string(String(repeating: "a", count: 64)),
                "package_path": .string("tmp/capture-native-root-abc"),
                "group": .object(["group_id": .string("group-a")]),
                "selected_suite": .object(["suite_id": .string("synthetic-package")]),
                "entries": .array([.object([:]), .object([:])]),
            ]),
        ]))
        #expect(model.packagePreparation?.phase == .prepared && model.packagePreparation?.entryCount == 2)
        // A late duplicate of an earlier state cannot unwind the terminal result.
        model.applyPackageEvent(preparationEvent("running", operation: "prepare-1"))
        #expect(model.packagePreparation?.phase == .prepared)
    }

    @Test @MainActor func retriedAttemptsAreCountedAndForeignEventsAreIgnored() {
        let model = preparedModel()
        model.openPackageSelection()
        model.seedPackagePreparation(operationID: "prepare-1", groupID: "group-a")
        for _ in 0..<3 {
            model.applyPackageEvent(preparationEvent("queued", operation: "prepare-1"))
            model.applyPackageEvent(preparationEvent("running", operation: "prepare-1"))
        }
        #expect(model.packagePreparation?.attempts == 3)
        model.applyPackageEvent(preparationEvent("failed", operation: "prepare-1",
                                                 code: "RECORDING.WRITE_FAILED",
                                                 message: "package worker stopped before completion; retry preparation",
                                                 retryable: true))
        #expect(model.packagePreparation?.phase == .failed && model.packagePreparation?.retryable == true)
        // Another operation's events and another group's package never touch this state.
        model.applyPackageEvent(preparationEvent("queued", operation: "prepare-other"))
        #expect(model.packagePreparation?.phase == .failed)
        model.applyPackageEvent(.object([
            "event": .string("package"),
            "package": .object(["group": .object(["group_id": .string("group-b")]), "entries": .array([])]),
        ]))
        #expect(model.packagePreparation?.phase == .failed)
    }

    @Test @MainActor func changingTheSelectionRetiresItsDurableOperationIdentity() {
        let model = preparedModel()
        model.openPackageSelection()
        model.seedPackagePreparation(operationID: "prepare-1", groupID: "group-a")
        model.togglePackageSelection("take-two")
        #expect(model.packageOperationIdentity == nil)
        // Events for the retired operation are ignored once the selection changed.
        model.applyPackageEvent(preparationEvent("running", operation: "prepare-1"))
        #expect(model.packagePreparation?.phase == .queued)
    }
}
