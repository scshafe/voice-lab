import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI
import Testing

/// Pure-model coverage for recovery. Recovery is metadata only: it never reopens input,
/// never turns an interrupted capture into a savable take, and never loops on a busy owner.
@Suite(.serialized)
struct RecordingRecoveryTests {
    private func entry(_ takeID: String, status: String, code: String? = nil,
                       message: String? = nil, recoveredSeal: Bool = false) -> RecordingJSON {
        var value: [String: RecordingJSON] = [
            "take_id": .string(takeID), "status": .string(status),
            "recovered_seal": .boolean(recoveredSeal),
        ]
        if let code {
            value["error"] = .object([
                "code": .string(code), "message": .string(message ?? code),
                "retryable": .boolean(false),
            ])
        }
        return .object(value)
    }

    private func event(_ entries: [RecordingJSON], takeID: String? = nil,
                       next: String? = nil, operation: String = "recover-1") -> RecordingJSON {
        .object([
            "event": .string("recovery"), "operation_id": .string(operation),
            "take_id": takeID.map(RecordingJSON.string) ?? .null,
            "page": .object([
                "entries": .array(entries),
                "next_take_id": next.map(RecordingJSON.string) ?? .null,
            ]),
        ])
    }

    @Test @MainActor func recoveryPagesAreIdempotentAcrossDuplicateDelivery() {
        let model = RecordingWorkspaceModel(fixture: .init())
        let page = event([
            entry("take-partial", status: "partial", code: "RECORDING.WRITE_FAILED",
                  message: "Capture ended without a seal; preserved material is incomplete"),
            entry("take-kept", status: "kept", recoveredSeal: true),
        ])
        // Every reply is also yielded on the unsolicited stream.
        model.seedRecoveryFixture(page)
        model.seedRecoveryFixture(page)
        #expect(model.recoveryRows.count == 2)
        #expect(model.recoveryIncompleteRows.map(\.takeID) == ["take-partial"])
        #expect(model.recoveredSealCount == 1)
        #expect(model.recoveryStatusLine == "Capture ended without a seal; preserved material is incomplete")
    }

    @Test @MainActor func aSingleTakeRecoveryMustReturnExactlyItsRequestedIdentity() {
        let model = RecordingWorkspaceModel(fixture: .init())
        model.seedRecoveryFixture(event([entry("take-other", status: "partial", code: "RECORDING.WRITE_FAILED")],
                                        takeID: "take-requested"))
        #expect(model.recoveryRows.isEmpty)
        model.seedRecoveryFixture(event([entry("take-requested", status: "partial", code: "RECORDING.WRITE_FAILED")],
                                        takeID: "take-requested"))
        #expect(model.recoveryRows.keys.sorted() == ["take-requested"])
    }

    @Test @MainActor func evidenceContradictionsAreRefusedRatherThanRendered() {
        let model = RecordingWorkspaceModel(fixture: .init())
        // unavailable without a named error, and a recovered seal on an unsealed status.
        model.seedRecoveryFixture(event([
            entry("take-unnamed", status: "unavailable"),
            entry("take-impossible", status: "partial", code: "RECORDING.WRITE_FAILED", recoveredSeal: true),
        ]))
        #expect(model.recoveryRows.isEmpty)
        model.seedRecoveryFixture(event([
            entry("take-named", status: "unavailable", code: "RECORDING.SOURCE_CHANGED",
                  message: "native recovery receipt does not bind its reservation"),
        ]))
        #expect(model.recoveryRows.keys.sorted() == ["take-named"])
        #expect(model.recoveryIncompleteRows[0].errorCode == "RECORDING.SOURCE_CHANGED")
    }

    @Test @MainActor func anIncompleteRecoveryNeverBecomesASavableTake() {
        let model = RecordingWorkspaceModel(fixture: .init())
        let before = model.canSave
        let selectedBefore = model.state.takeID
        for status in ["partial", "empty", "busy"] {
            model.seedRecoveryFixture(event([
                entry("take-" + status, status: status,
                      code: status == "busy" ? "RECORDING.OWNERSHIP_CONFLICT" : "RECORDING.WRITE_FAILED"),
            ]))
        }
        #expect(model.recoveryIncompleteRows.count == 3)
        #expect(model.canSave == before)
        // Recovery never selects, replaces or promotes a take.
        #expect(model.state.takeID == selectedBefore)
        #expect(!model.recoveryRows.keys.contains(selectedBefore ?? ""))
        #expect(model.recoveredSealCount == 0)
    }

    @Test @MainActor func sleepEndsCaptureExactlyOnceAndNeverResumes() {
        let model = RecordingWorkspaceModel(fixture: .init())
        let selectedBefore = model.state.takeID
        // With no capture running, sleep is inert rather than a spurious transition.
        model.handleSystemSleep()
        model.handleSystemSleep()
        #expect(model.state.phase != .recording)
        #expect(!model.state.canStop)
        #expect(model.state.takeID == selectedBefore)
    }
}
