import Foundation
import RecordingContracts
@testable import RecordingHarness
@testable import RecordingUI
import Testing

@Suite(.serialized)
struct RecordingReviewObserverTests {
    @Test @MainActor func failedSaveInDraftReviewRejectsPendingWaitWithItsActualError() async throws {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .saving))
        let observer = ReviewObserver(model, barrierSeconds: 1)
        let waiting = Task { @MainActor in try await observer.wait { model.state.phase == .ready } }
        await Task.yield()
        publishFailedSave(model)
        do {
            try await waiting.value
            Issue.record("A rejected Save passed the ordinary review barrier")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.HISTORY_UNKNOWN")
            #expect(error.message == "History is still building; Save and Next is unavailable")
            #expect(error.context["phase"] == "draftReview")
            #expect(error.context["history_completeness"] == "building")
            #expect(model.state.phase == .draftReview)
            #expect(model.state.errorCode == error.code)
            let checkpoint = ReviewObserver.checkpoint(model, label: "command-failure")
            let retained = try RecordingJSON.parse(checkpoint.transportData())
            #expect(retained.object?["error_code"] == .string(error.code))
            #expect(retained.object?["phase"] == .string("draftReview"))
            #expect(retained.object?["history_completeness"] == .string("building"))
        }
    }

    @Test @MainActor func namedErrorTakesPrecedenceOverAnAlreadySatisfiedPredicate() async throws {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .draftReview))
        publishFailedSave(model)
        let observer = ReviewObserver(model, barrierSeconds: 1)
        do {
            try await observer.wait { true }
            Issue.record("An apparently ready predicate erased the command error")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.HISTORY_UNKNOWN")
        }
    }

    @Test @MainActor func explicitlyExpectedFailureStillCompletesWithoutErasingTheError() async throws {
        let model = RecordingWorkspaceModel(fixture: .init(phase: .draftReview))
        publishFailedSave(model)
        let observer = ReviewObserver(model, barrierSeconds: 1)
        try await observer.waitForFailure(code: "RECORDING.HISTORY_UNKNOWN")
        #expect(model.state.errorCode == "RECORDING.HISTORY_UNKNOWN")
        #expect(model.state.phase == .draftReview)
    }

    @MainActor private func publishFailedSave(_ model: RecordingWorkspaceModel) {
        var state = model.state
        state.phase = .draftReview
        state.historyCompleteness = "building"
        state.errorCode = "RECORDING.HISTORY_UNKNOWN"
        state.status = "History is still building; Save and Next is unavailable"
        model.publish(state)
    }
}
