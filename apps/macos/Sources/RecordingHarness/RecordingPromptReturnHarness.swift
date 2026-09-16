import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI

/// Holds the next load of one take in the actual offline player, so a scenario can press
/// Record exactly while an automatic review of that take is still loading.
actor RecordingHeldReviewBackend: RecordingPlaybackBackend {
    let base: AVFoundationRecordingPlaybackBackend
    private var heldTakeID: String?
    private var entered = false
    private var enteredWaiters: [CheckedContinuation<Void, Never>] = []
    private var release: CheckedContinuation<Void, Never>?

    init(base: AVFoundationRecordingPlaybackBackend) { self.base = base }

    func hold(takeID: String) { heldTakeID = takeID; entered = false }
    func waitUntilHeld() async {
        guard !entered else { return }
        await withCheckedContinuation { enteredWaiters.append($0) }
    }
    func releaseHeld() { release?.resume(); release = nil }

    func setEventHandler(_ handler: @escaping @Sendable (RecordingPlaybackBackendEvent) async -> Void) async {
        await base.setEventHandler(handler)
    }
    func load(_ source: RecordingPlaybackSource, generation: UInt64) async throws {
        try await load(source, generation: generation, diagnosticContext: nil)
    }
    func load(_ source: RecordingPlaybackSource, generation: UInt64, diagnosticContext: RecordingDiagnosticContext?) async throws {
        if source.takeID == heldTakeID {
            heldTakeID = nil
            await withCheckedContinuation { continuation in
                release = continuation; entered = true
                for waiter in enteredWaiters { waiter.resume() }
                enteredWaiters.removeAll()
            }
        }
        try await base.load(source, generation: generation, diagnosticContext: diagnosticContext)
    }
    func play(from frame: Int64, through endFrame: Int64, generation: UInt64) async throws {
        try await base.play(from: frame, through: endFrame, generation: generation)
    }
    func pause() async throws -> Int64? { try await base.pause() }
    func position() async -> Int64? { await base.position() }
    func stop() async throws { try await base.stop() }
    func recover() async throws { try await base.recover() }
}

/// One-shot startup barrier inside Record, after capture owns playback and before reservation.
@MainActor final class RecordingStartupHold {
    private var armed = false
    private var entered = false
    private var enteredWaiter: CheckedContinuation<Void, Never>?
    private var gate: CheckedContinuation<Void, Never>?

    func arm() { armed = true; entered = false }
    func pass() async {
        guard armed else { return }
        armed = false
        await withCheckedContinuation { continuation in
            gate = continuation; entered = true
            enteredWaiter?.resume(); enteredWaiter = nil
        }
    }
    func waitUntilEntered() async {
        guard !entered else { return }
        await withCheckedContinuation { enteredWaiter = $0 }
    }
    func release() { gate?.resume(); gate = nil }
}

/// Counts finished workspace review spans; the harness observes a superseded load resolving.
final class RecordingReviewSpanLog: @unchecked Sendable {
    private let lock = NSLock()
    private var ended = 0
    var sink: RecordingDiagnosticSink {
        { [self] event in
            guard event.name == "workspace.review", event.phase == .end else { return }
            lock.lock(); ended += 1; lock.unlock()
        }
    }
    var endedCount: Int { lock.lock(); defer { lock.unlock() }; return ended }
}

extension RecordingHarness {
    /// Physical P06: an explicit return to a recorded prompt selects its latest retained take,
    /// stopped and playable. Record pressed while that review is still loading supersedes it
    /// without a review or playback failure, and a start cancelled before its reservation
    /// shows the same take again.
    @MainActor static func reviewPromptReturn(model: RecordingWorkspaceModel, observer: ReviewObserver,
                                              backend: RecordingHeldReviewBackend, startup: RecordingStartupHold,
                                              spans: RecordingReviewSpanLog, keptTakeID: String) async throws {
        model.selectPrompt("reading-two")
        try await observer.wait { model.state.prompt?.id == "reading-two" && !model.state.navigationPending }
        guard model.state.takeID == nil, model.selectedReview == nil else { throw promptReturnError("A prompt without a retained take must select none") }

        model.selectPrompt("reading-one")
        try await observer.wait { model.state.prompt?.id == "reading-one" && !model.state.navigationPending }
        guard model.state.takeID == keptTakeID else { throw promptReturnError("Returning to a recorded prompt must select its latest retained take") }
        try await observer.wait { model.selectedReview?.playback.takeID == keptTakeID && !model.review.loading }
        guard model.selectedDecision == "kept", model.review.playback.phase == .stopped, model.canPlaySelected,
              model.state.phase == .ready, model.canRecord, !model.canSave else {
            throw promptReturnError("A returned kept take must be playable without autoplay, Record-ready and never savable")
        }

        model.selectPrompt("reading-two")
        try await observer.wait { model.state.prompt?.id == "reading-two" && !model.state.navigationPending && model.state.takeID == nil }
        await backend.hold(takeID: keptTakeID)
        model.selectPrompt("reading-one")
        await backend.waitUntilHeld()
        guard model.state.takeID == keptTakeID, model.review.loading, model.canRecord else {
            throw promptReturnError("The returned take's automatic review must still be loading while Record is available")
        }
        let reviewsEnded = spans.endedCount
        startup.arm()
        model.record()
        await startup.waitUntilEntered()
        guard model.review.playback.captureOwned, !model.review.loading else { throw promptReturnError("Record must supersede the loading review before capture") }
        await backend.releaseHeld()
        // The held load now resolves against a newer transport generation; wait for that review.
        var polls = 0
        while spans.endedCount == reviewsEnded, polls < 3_000 { try await Task.sleep(for: .milliseconds(10)); polls += 1 }
        guard spans.endedCount > reviewsEnded else { throw promptReturnError("The superseded review never resolved") }
        guard model.review.failure == nil, model.review.playback.failure == nil else {
            throw promptReturnError("A review superseded by Record reported a failure: \(model.review.failure?.message ?? model.review.playback.failure?.message ?? "")")
        }
        model.stop()
        startup.release()
        try await observer.wait {
            model.state.phase == .ready && model.state.takeID == keptTakeID &&
            model.selectedReview?.playback.takeID == keptTakeID && !model.review.loading
        }
        guard model.review.playback.phase == .stopped, model.canPlaySelected, model.canRecord,
              model.state.prompt?.id == "reading-one" else {
            throw promptReturnError("A start cancelled before reservation must show its selected take again, stopped")
        }
    }

    private static func promptReturnError(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.REVIEW_HARNESS", message: message)
    }
}
