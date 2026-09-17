import Combine
import Foundation
import RecordingContracts

/// @canonical recording_playback -- review transport and capture exclusion.
/// Cursor values are source frames from the backend player clock, never elapsed wall time.
@MainActor public final class RecordingPlaybackController: ObservableObject {
    public enum Phase: String, Sendable { case empty, loading, starting, stopped, playing, paused, failed }
    @Published public private(set) var phase: Phase = .empty
    @Published public private(set) var source: RecordingPlaybackSource?
    @Published public private(set) var retainedRange: Range<Int64> = 0..<1
    @Published public private(set) var cursorFrame: Int64 = 0
    @Published public private(set) var failure: RecordingContractError?
    public private(set) var captureOwned = false
    public private(set) var generation: UInt64 = 0
    private let backend: any RecordingPlaybackBackend
    private var eventHandlerInstalled = false
    private var closing = false
    @Published private var pendingGeneration: UInt64?
    private var cursorMemory: [String: Int64] = [:]
    private var cursorOrder: [String] = []
    private let cursorCapacity = 200

    public init(backend: any RecordingPlaybackBackend) { self.backend = backend }

    public var canPlay: Bool { !captureOwned && pendingGeneration == nil && failure == nil && source != nil && [.stopped, .paused, .playing].contains(phase) }

    /// Keep the old display coherent until this selected source finishes loading.
    public func select(_ newSource: RecordingPlaybackSource, retainedRange newRange: Range<Int64>, cursor: Int64? = nil,
                       diagnosticContext: RecordingDiagnosticContext? = nil) async throws {
        try validate(newRange, sourceFrames: newSource.frameCount)
        guard !captureOwned else { throw unavailable("Playback selection is unavailable during capture") }
        rememberCursor()
        let token = advanceGeneration()
        phase = .loading
        failure = nil
        await installEventHandler()
        do {
            try await backend.load(newSource, generation: token, diagnosticContext: diagnosticContext)
            guard token == generation, !captureOwned else { throw cancelled() }
            source = newSource
            retainedRange = newRange
            cursorFrame = clamped(cursor ?? cursorMemory[newSource.takeID] ?? newRange.lowerBound)
            phase = .stopped
        } catch {
            guard token == generation else { throw cancelled() }
            recordFailure(error)
            throw error
        }
    }

    public func playPause() async throws {
        guard !closing, canPlay else { throw unavailable("Selected audio is not ready for playback") }
        let token = advanceGeneration()
        pendingGeneration = token
        defer { if pendingGeneration == token { pendingGeneration = nil } }
        do {
            if phase == .playing {
                if let frame = try await backend.pause(), token == generation { cursorFrame = clamped(frame) }
                guard token == generation else { return }
                phase = .paused
                rememberCursor()
            } else {
                let start = cursorFrame == retainedRange.upperBound ? retainedRange.lowerBound : cursorFrame
                cursorFrame = start
                phase = .starting
                try await backend.play(from: start, through: retainedRange.upperBound, generation: token)
                guard token == generation, !captureOwned, phase == .starting else { return }
                phase = .playing
            }
        } catch {
            if token == generation { recordFailure(error) }
            throw error
        }
    }

    /// Pause output that is playing or starting (another page or transport takes over); any
    /// other state is left as it is. The cursor stays where the output stopped.
    public func pause() async throws {
        guard phase == .playing || phase == .starting else { return }
        let token = advanceGeneration()
        pendingGeneration = token
        defer { if pendingGeneration == token { pendingGeneration = nil } }
        do {
            if let frame = try await backend.pause(), token == generation { cursorFrame = clamped(frame) }
            guard token == generation else { return }
            phase = .paused
            rememberCursor()
        } catch { if token == generation { recordFailure(error) }; throw error }
    }

    /// Seek never starts stopped audio. Active playback reschedules its exact segment.
    public func seek(to frame: Int64) async throws {
        guard !closing, canPlay else { throw unavailable("Selected audio is not ready for seeking") }
        let wasPlaying = phase == .playing
        let target = clamped(frame)
        let token = advanceGeneration()
        pendingGeneration = token
        defer { if pendingGeneration == token { pendingGeneration = nil } }
        do {
            try await backend.stop()
            guard token == generation else { return }
            cursorFrame = target
            phase = .stopped
            rememberCursor()
            if wasPlaying && target < retainedRange.upperBound {
                phase = .starting
                try await backend.play(from: target, through: retainedRange.upperBound, generation: token)
                guard token == generation, !captureOwned, phase == .starting else { return }
                phase = .playing
            }
        } catch { if token == generation { recordFailure(error) }; throw error }
    }

    public func skip(frames: Int64) async throws {
        let result = cursorFrame.addingReportingOverflow(frames)
        try await seek(to: result.overflow ? (frames > 0 ? retainedRange.upperBound : retainedRange.lowerBound) : result.partialValue)
    }

    /// The service owns durable edit rules; this is only a bounded preview range.
    public func previewRange(_ range: Range<Int64>) async throws {
        guard let source, canPlay else { throw unavailable("No stopped source is ready for trimming") }
        try validate(range, sourceFrames: source.frameCount)
        let token = advanceGeneration()
        pendingGeneration = token
        defer { if pendingGeneration == token { pendingGeneration = nil } }
        do { try await backend.stop() }
        catch { if token == generation { recordFailure(error) }; throw error }
        guard token == generation else { return }
        retainedRange = range
        cursorFrame = clamped(cursorFrame)
        phase = .stopped
        rememberCursor()
    }

    public func refreshPosition() async {
        guard phase == .playing else { return }
        let token = generation
        if let frame = await backend.position(), token == generation, phase == .playing {
            cursorFrame = clamped(frame)
        }
    }

    /// Capture may proceed only after output shutdown has succeeded.
    public func beginCapture() async throws {
        let token = advanceGeneration()
        pendingGeneration = token
        defer { if pendingGeneration == token { pendingGeneration = nil } }
        do {
            if let position = await backend.position(), token == generation { cursorFrame = clamped(position) }
            guard token == generation else { throw cancelled() }
            rememberCursor()
            try await backend.stop()
            guard token == generation else { throw cancelled() }
            guard failure == nil else { throw unavailable("Recover failed playback before recording") }
            captureOwned = true
            phase = source == nil ? .empty : .stopped
        } catch { if token == generation { recordFailure(error) }; throw error }
    }

    public func endCapture() { captureOwned = false }

    public func recover() async throws {
        guard !captureOwned else { throw unavailable("Capture owns the audio transport") }
        let token = advanceGeneration()
        phase = .loading
        do {
            try await backend.recover()
            guard token == generation else { return }
            if let source { try await backend.load(source, generation: token) }
        } catch { if token == generation { recordFailure(error) }; throw error }
        guard token == generation else { return }
        failure = nil
        phase = source == nil ? .empty : .stopped
    }

    public func shutdown() async throws {
        let token = advanceGeneration()
        // Close and Quit refuse to start output while the existing output is stopping.
        // Operations that only stop output stay available; they cannot outlive the close.
        closing = true
        defer { closing = false }
        do { try await backend.stop() }
        catch { if token == generation { recordFailure(error) }; throw error }
        guard token == generation else { return }
        rememberCursor()
        phase = source == nil ? .empty : .stopped
    }

    private func installEventHandler() async {
        guard !eventHandlerInstalled else { return }
        eventHandlerInstalled = true
        await backend.setEventHandler { [weak self] event in
            await self?.receive(event)
        }
    }

    private func receive(_ event: RecordingPlaybackBackendEvent) {
        switch event {
        case .finished(let token):
            guard token == generation, [.starting, .playing].contains(phase), !captureOwned else { return }
            cursorFrame = retainedRange.upperBound
            phase = .stopped
            rememberCursor()
        case .failed(let token, let error):
            guard token == generation else { return }
            recordFailure(error)
        }
    }

    private func advanceGeneration() -> UInt64 { generation &+= 1; pendingGeneration = nil; return generation }
    private func clamped(_ frame: Int64) -> Int64 { min(retainedRange.upperBound, max(retainedRange.lowerBound, frame)) }
    private func rememberCursor() {
        guard let source else { return }
        cursorOrder.removeAll { $0 == source.takeID }
        cursorOrder.append(source.takeID)
        cursorMemory[source.takeID] = cursorFrame
        if cursorOrder.count > cursorCapacity { cursorMemory.removeValue(forKey: cursorOrder.removeFirst()) }
    }
    private func validate(_ range: Range<Int64>, sourceFrames: Int64) throws {
        guard range.lowerBound >= 0, range.lowerBound < range.upperBound, range.upperBound <= sourceFrames else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Preview range must be nonempty and inside its original source")
        }
    }
    private func recordFailure(_ error: any Error) {
        failure = error as? RecordingContractError ?? RecordingContractError(code: "RECORDING.PLAYBACK_FAILED", message: "Playback failed; recover audio before recording")
        phase = .failed
    }
    private func unavailable(_ message: String) -> RecordingContractError { RecordingContractError(code: "RECORDING.PLAYBACK_ACTIVE", message: message) }
    private func cancelled() -> RecordingContractError { RecordingContractError(code: "RECORDING.CANCELLED", message: "Playback selection was superseded") }
}
