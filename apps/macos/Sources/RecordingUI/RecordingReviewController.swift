import Foundation
import RecordingContracts
import RecordingCore

/// One selected review source, bounded cache and explicit unsaved frame-range preview.
@MainActor
public final class RecordingReviewController: ObservableObject {
    public struct Prepared: Sendable {
        let source: RecordingReviewSource
        let cache: RecordingWaveformCache
        let diagnosticContext: RecordingDiagnosticContext?
    }
    public let playback: RecordingPlaybackController
    @Published public private(set) var source: RecordingReviewSource?
    @Published public private(set) var loading = false
    @Published public private(set) var failure: RecordingContractError?
    @Published public private(set) var trimRange: Range<Int64> = 0..<1
    @Published public private(set) var startText = "0"
    @Published public private(set) var endText = "0"
    @Published public private(set) var fieldError: String?
    @Published public private(set) var viewport: Range<Int64>?
    @Published public var fitDisplay = false
    @Published public private(set) var cacheStatus = "unknown"
    public private(set) var generation: UInt64 = 0
    private var cache: RecordingWaveformCache?
    private var loadTask: Task<RecordingWaveformCache, Error>?
    private var previewTask: Task<Void, Never>?
    private let archives = RecordingWaveformArchiveQueue()
    private let diagnosticSink: RecordingDiagnosticSink?

    public init(backend: any RecordingPlaybackBackend = AVFoundationRecordingPlaybackBackend(), diagnosticSink: RecordingDiagnosticSink? = nil) {
        self.diagnosticSink = diagnosticSink
        playback = RecordingPlaybackController(backend: backend)
    }
    public var isDirty: Bool { source.map { trimRange != $0.retainedRange || fieldError != nil } ?? false }
    public var canReview: Bool { source != nil && !loading && failure == nil && !playback.captureOwned }
    public var canPlay: Bool { canReview && playback.canPlay && fieldError == nil }

    /// Keep the displayed source until both exact playback and waveform dependencies are ready.
    public func prepare(_ selected: RecordingReviewSource, processGeneration: String,
                        diagnosticContext: RecordingDiagnosticContext? = nil) async throws -> Prepared {
        let context = diagnosticSink == nil ? nil : (diagnosticContext ?? RecordingDiagnosticContext(traceID: UUID().uuidString.lowercased()))
            .source(selected.playback, preparationID: UUID().uuidString.lowercased())
        let span = RecordingDiagnosticSpan("review.prepare", context: context, sink: diagnosticSink)
        generation &+= 1; let token = generation
        loadTask?.cancel(); loading = true; failure = nil
        let sink = diagnosticSink
        let task = Task.detached(priority: .userInitiated) {
            try RecordingWaveformSelectionLoader.load(selected.playback, processGeneration: processGeneration,
                diagnosticContext: span.childContext, diagnosticSink: sink)
        }
        loadTask = task
        do {
            async let player: Void = playback.select(selected.playback, retainedRange: selected.retainedRange, diagnosticContext: span.childContext)
            let loaded = try await task.value
            try await player
            guard token == generation else { throw cancelled() }
            loadTask = nil
            span.finish()
            return Prepared(source: selected, cache: loaded, diagnosticContext: span.childContext)
        } catch {
            span.finish(error is CancellationError ? .cancelled : .failure, error: error)
            if token == generation { loading = false; loadTask = nil; report(error) }
            throw error
        }
    }

    /// Publish only after the workspace has checked its operation and selected take identity.
    public func commit(_ prepared: Prepared, waveform: RecordingWaveformModel) throws {
        let span = RecordingDiagnosticSpan("review.commit", context: prepared.diagnosticContext, sink: diagnosticSink)
        do { try install(prepared, waveform: waveform); span.finish() }
        catch { span.finish(.failure, error: error); throw error }
    }

    private func install(_ prepared: Prepared, waveform: RecordingWaveformModel) throws {
        let snapshot = try prepared.cache.snapshot()
        source = prepared.source; cache = prepared.cache
        trimRange = prepared.source.retainedRange
        startText = String(trimRange.lowerBound); endText = String(trimRange.upperBound)
        fieldError = nil; viewport = nil; loading = false; failure = nil
        waveform.install(snapshot, hostClockComparable: false)
        archives.enqueue(takeID: prepared.source.playback.takeID, sourceSHA256: prepared.source.playback.sourceSHA256,
            assetRoot: prepared.source.playback.assetRoot, makeArchive: { [cache = prepared.cache] in
                { try cache.archive(sourceSHA256: prepared.source.playback.sourceSHA256, finalWrittenFrames: prepared.source.playback.frameCount) }
            }, completion: { [weak self] completion in
                Task { @MainActor in
                    guard let self, self.source?.playback.takeID == completion.takeID else { return }
                    switch completion.result {
                    case .stored: self.cacheStatus = "ready"
                    case .needsRebuild: self.cacheStatus = "needs-rebuild"
                    }
                }
            })
    }

    public func updateAuthorization(_ authorized: RecordingReviewSource) throws {
        guard let current = source, current.playback.takeID == authorized.playback.takeID,
              current.playback.sourceSHA256 == authorized.playback.sourceSHA256,
              current.playback.frameCount == authorized.playback.frameCount else {
            throw RecordingContractError(code: "RECORDING.STALE_REVISION", message: "Edit reply differs from the selected original")
        }
        source = authorized; trimRange = authorized.retainedRange
        startText = String(trimRange.lowerBound); endText = String(trimRange.upperBound); fieldError = nil
        drainPreview()
    }

    public func cancelPreparation(generation token: UInt64) {
        guard token == generation else { return }
        generation &+= 1; loadTask?.cancel(); loadTask = nil; loading = false
    }

    public func discardDisplay() {
        generation &+= 1; loadTask?.cancel(); loadTask = nil; previewTask?.cancel(); previewTask = nil
        source = nil; cache = nil; cacheStatus = "unknown"; loading = false; failure = nil; fieldError = nil; viewport = nil
        trimRange = 0..<1; startText = "0"; endText = "0"
    }

    public func setStartText(_ text: String) { startText = text; validateFields() }
    public func setEndText(_ text: String) { endText = text; validateFields() }
    private func validateFields() {
        guard let source else { return }
        guard !startText.isEmpty, !endText.isEmpty,
              startText.allSatisfy(\.isNumber), endText.allSatisfy(\.isNumber),
              let start = Int64(startText), let end = Int64(endText),
              start >= 0, start < end, end <= source.playback.frameCount else {
            fieldError = "Use whole source frames: 0 ≤ In < Out ≤ \(source.playback.frameCount)"; return
        }
        fieldError = nil; trimRange = start..<end; drainPreview()
    }

    public func preview(_ range: Range<Int64>) {
        guard let source, range.lowerBound >= 0, range.lowerBound < range.upperBound,
              range.upperBound <= source.playback.frameCount, canReview else { return }
        trimRange = range; startText = String(range.lowerBound); endText = String(range.upperBound)
        fieldError = nil; drainPreview()
    }
    private func drainPreview() {
        guard previewTask == nil else { return }
        let token = generation
        previewTask = Task { [weak self] in
            guard let self else { return }
            defer { previewTask = nil }
            do {
                while token == generation, let source, playback.source?.takeID == source.playback.takeID,
                      trimRange != playback.retainedRange {
                    let next = trimRange
                    try await playback.previewRange(next)
                }
            } catch { if token == generation { report(error) } }
        }
    }

    public func resetPreview() {
        guard let source else { return }; preview(0..<source.playback.frameCount)
    }
    public func discardPreview() {
        guard let source else { return }; preview(source.retainedRange)
    }
    public func zoom(_ factor: Int, waveform: RecordingWaveformModel) throws {
        guard let source, let cache, canReview else { return }
        let full = source.playback.frameCount
        let previous = viewport ?? 0..<full
        let width = factor > 0 ? min(Int(full), max(64, previous.count / 2)) : min(Int(full), previous.count * 2)
        let center = playback.cursorFrame
        let start = max(0, min(full - Int64(width), center - Int64(width / 2)))
        viewport = start..<(start + Int64(width))
        _ = waveform.accept(try cache.snapshot(range: viewport))
    }
    public func report(_ error: Error) {
        failure = error as? RecordingContractError ?? RecordingContractError(code: "RECORDING.PLAYBACK_FAILED", message: "Selected take review failed")
    }
    private func cancelled() -> RecordingContractError {
        RecordingContractError(code: "RECORDING.CANCELLED", message: "Selected review was superseded")
    }
}
