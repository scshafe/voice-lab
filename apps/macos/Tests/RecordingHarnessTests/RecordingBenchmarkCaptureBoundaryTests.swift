import Foundation
import RecordingContracts
import RecordingUI
@testable import RecordingHarness
import Testing

@Suite(.serialized)
struct RecordingBenchmarkCaptureBoundaryTests {
    @Test func initialReviewUsesActualTimerDurationWhilePreservingEarlierLiveFrames() {
        #expect(RecordingMediaTimer.durationFrames(reviewing: false, writtenFrames: 480, retainedRange: 0..<600) == 480)
        #expect(RecordingMediaTimer.durationFrames(reviewing: true, writtenFrames: 480, retainedRange: 0..<600) == 600)
        #expect(RecordingMediaTimer.durationFrames(reviewing: true, writtenFrames: 600, retainedRange: 120..<480) == 360)
    }

    @Test func maximumDurationPolicyAcceptsOnlyTheDeclaredAutomaticLimit() {
        let automatic = RecordingBenchmarkCaptureBoundary.automaticLimitObservation(state(phase: "stopping", frames: 28_798_080))
        for frames in [Int64(28_798_080), 28_800_000] {
            #expect(RecordingBenchmarkCaptureBoundary.completionSucceeded(policy: .maximumDurationLimit,
                expected: 28_800_000, maximum: 28_800_000, sealed: frames,
                terminalReason: "maximum-duration", timedOut: false, stop: automatic))
        }
        let invalidCases: [(Int64, Int64, String, Bool)] = [
            (28_800_000, 0, "maximum-duration", false),
            (28_800_000, 28_800_001, "maximum-duration", false),
            (28_800_000, 28_798_080, "requested", false),
            (28_800_000, 28_798_080, "maximum-duration", true),
            (48_000, 48_000, "maximum-duration", false),
        ]
        for (expected, frames, reason, timeout) in invalidCases {
            #expect(!RecordingBenchmarkCaptureBoundary.completionSucceeded(policy: .maximumDurationLimit,
                expected: expected, maximum: 28_800_000, sealed: frames,
                terminalReason: reason, timedOut: timeout, stop: automatic))
        }
        #expect(!RecordingBenchmarkCaptureBoundary.completionSucceeded(policy: .exactFrames,
            expected: 28_800_000, maximum: 28_800_000, sealed: 28_798_080,
            terminalReason: "maximum-duration", timedOut: false, stop: automatic))
        #expect(automatic.issued == nil && !automatic.modelStopCalled && !automatic.modelStopAccepted)
    }

    private func state(phase: String = "recording", frames: Int64 = 480,
                       take: String = "take", generation: String = "generation") -> RecordingBenchmarkCaptureState {
        RecordingBenchmarkCaptureState(takeID: take, generation: generation, phase: phase,
            canStop: phase == "recording" || phase == "starting", writtenFrames: frames)
    }

    @Test func finalFrameTargetCannotBeInferredFromSuccessfulWaitOrTerminalState() {
        #expect(!RecordingBenchmarkCaptureBoundary.reachedConfiguredFrames(expected: 28_800_000, sealed: 28_798_080))
        #expect(RecordingBenchmarkCaptureBoundary.reachedConfiguredFrames(expected: 28_800_000, sealed: 28_800_000))
        #expect(!RecordingBenchmarkCaptureBoundary.reachedConfiguredFrames(expected: 480, sealed: 600))
        #expect(!RecordingBenchmarkCaptureBoundary.reachedConfiguredFrames(expected: 0, sealed: 0))
        #expect(RecordingBenchmarkCaptureBoundary.waitExit(expected: 28_800_000, observed: 28_793_280, timedOut: false) == "terminal_before_target")
        #expect(RecordingBenchmarkCaptureBoundary.waitExit(expected: 480, observed: 480, timedOut: false) == "frame_target_observed")
        #expect(RecordingBenchmarkCaptureBoundary.waitExit(expected: 480, observed: 400, timedOut: true) == "timeout")
    }

    @Test @MainActor func alreadyTerminalDoesNotIssueClockOrCallStop() async {
        var clocks = 0
        var calls = 0
        let result = await RecordingBenchmarkCaptureBoundary.issueStop(takeID: "take", generation: "generation",
            state: { state(phase: "stopping", frames: 28_793_280) },
            issuedUptime: { clocks += 1; return 100 }, dispatch: { calls += 1 })
        #expect(clocks == 0 && calls == 0)
        #expect(result.issued == nil && !result.modelStopAccepted)
        #expect(result.reason == "terminal_before_issue")
        #expect(result.before.writtenFrames == 28_793_280)
    }

    @Test @MainActor func clockAwaitCannotTurnAutomaticStopIntoCommandLatency() async {
        var current = state()
        var calls = 0
        let result = await RecordingBenchmarkCaptureBoundary.issueStop(takeID: "take", generation: "generation",
            state: { current }, issuedUptime: { current = state(phase: "stopping", frames: 600); return 100 },
            dispatch: { calls += 1 })
        #expect(calls == 0 && !result.modelStopCalled && !result.modelStopAccepted)
        #expect(result.reason == "terminal_during_clock_await")
        #expect(result.before.writtenFrames == 480 && result.atIssue?.writtenFrames == 600)
    }

    @Test @MainActor func wrongGenerationOrTakeAfterClockNeverCallsStop() async {
        for changesTake in [false, true] {
            var current = state()
            var calls = 0
            let result = await RecordingBenchmarkCaptureBoundary.issueStop(takeID: "take", generation: "generation",
                state: { current }, issuedUptime: {
                    current = state(take: changesTake ? "other" : "take", generation: changesTake ? "generation" : "other")
                    return 100
                }, dispatch: { calls += 1 })
            #expect(calls == 0 && !result.modelStopAccepted)
            #expect(result.reason == "source_changed_during_clock_await")
        }
    }

    @Test @MainActor func actualModelStopKeepsOriginalAndAtIssueFrameObservations() async {
        var current = state()
        var calls = 0
        let result = await RecordingBenchmarkCaptureBoundary.issueStop(takeID: "take", generation: "generation",
            state: { current }, issuedUptime: { current = state(frames: 600); return 100 },
            dispatch: { calls += 1; current = state(phase: "stopping", frames: 600) })
        #expect(calls == 1 && result.modelStopCalled && result.modelStopAccepted)
        #expect(result.before.writtenFrames == 480 && result.atIssue?.writtenFrames == 600)
        #expect(result.json.object?["controller_acknowledgment_proven"] == .boolean(false))
    }

    @Test @MainActor func noOpDispatchCannotBecomeAnAcceptedStopWindow() async {
        let result = await RecordingBenchmarkCaptureBoundary.issueStop(takeID: "take", generation: "generation",
            state: { state() }, issuedUptime: { 100 }, dispatch: {})
        #expect(result.modelStopCalled && !result.modelStopAccepted)
        #expect(result.reason == "model_stop_did_not_transition")
    }
}
