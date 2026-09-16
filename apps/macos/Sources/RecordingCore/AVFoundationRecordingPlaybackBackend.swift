import AVFoundation
import Darwin
import Foundation
import RecordingContracts

/// File segments stream through one player node; the backend never retains decoded history.
/// Device output starts only in play(). Automated callers must choose offline rendering.
public actor AVFoundationRecordingPlaybackBackend: RecordingPlaybackBackend {
    public enum RenderingMode: Sendable { case device, offline }
    private let mode: RenderingMode
    private let diagnosticSink: RecordingDiagnosticSink?
    private var engine = AVAudioEngine()
    private var player = AVAudioPlayerNode()
    private var file: AVAudioFile?
    private var lease: RecordingPrivateFile.Lease?
    private var sourceRoot: URL?
    private var loading: Task<RecordingPrivateFile.Lease, any Error>?
    private var generation: UInt64 = 0
    private var loadRevision: UInt64 = 0
    private var segmentStart: Int64 = 0
    private var segmentEnd: Int64 = 0
    private var pausedFrame: Int64?
    private var playing = false
    private var handler: (@Sendable (RecordingPlaybackBackendEvent) async -> Void)?
    private var observer: RecordingPlaybackNotificationLease?
    private var graphGeneration = UUID()
    private var graphReady = false

    public init(mode: RenderingMode = .device, diagnosticSink: RecordingDiagnosticSink? = nil) {
        self.mode = mode; self.diagnosticSink = diagnosticSink
    }

    public func setEventHandler(_ handler: @escaping @Sendable (RecordingPlaybackBackendEvent) async -> Void) { self.handler = handler }

    public func load(_ source: RecordingPlaybackSource, generation: UInt64) async throws {
        try await load(source, generation: generation, diagnosticContext: nil)
    }

    public func load(_ source: RecordingPlaybackSource, generation: UInt64, diagnosticContext: RecordingDiagnosticContext?) async throws {
        let context = diagnosticSink == nil ? nil : (diagnosticContext ?? RecordingDiagnosticContext(traceID: UUID().uuidString.lowercased())).source(source)
        let span = RecordingDiagnosticSpan("playback.prepare", context: context, sink: diagnosticSink)
        do { try await prepare(source, generation: generation, diagnosticContext: span.childContext); span.finish() }
        catch { span.finish(error is CancellationError ? .cancelled : .failure, error: error); throw error }
    }

    private func prepare(_ source: RecordingPlaybackSource, generation: UInt64, diagnosticContext: RecordingDiagnosticContext?) async throws {
        try stop()
        self.generation = generation
        loadRevision &+= 1
        let revision = loadRevision
        file = nil; lease = nil; sourceRoot = nil
        let sink = diagnosticSink
        let task = Task.detached(priority: .userInitiated) {
            try Task.checkCancellation()
            try Self.validateAssetRoot(source.assetRoot)
            let path = source.assetRoot.appendingPathComponent(source.relativePath).path
            let lease = try RecordingPrivateFile.openReadOnly(path, maximumBytes: source.frameCount * 3 + 1_048_576,
                diagnosticContext: diagnosticContext, diagnosticSink: sink)
            if let expected = source.fileIdentity { try lease.validate(expectedIdentity: expected, relativePath: source.relativePath) }
            guard try lease.sha256() == source.sourceSHA256 else {
                throw RecordingContractError(code: "RECORDING.SOURCE_CHANGED", message: "Selected playback source no longer matches its checksum")
            }
            return lease
        }
        loading = task
        do {
            let verified = try await task.value
            guard revision == loadRevision, self.generation == generation else { throw cancelled() }
            let openSpan = RecordingDiagnosticSpan("playback.av_audio_file_open", context: diagnosticContext, sink: sink)
            let opened: AVAudioFile
            do {
                opened = try AVAudioFile(forReading: URL(fileURLWithPath: verified.path), commonFormat: .pcmFormatFloat32, interleaved: false)
                openSpan.finish()
            } catch { openSpan.finish(.failure, error: error); throw error }
            let format = opened.fileFormat.streamDescription.pointee
            guard opened.length == source.frameCount, format.mSampleRate == 48_000,
                  format.mChannelsPerFrame == 1, format.mBitsPerChannel == 24,
                  format.mFormatID == kAudioFormatLinearPCM else {
                throw RecordingContractError(code: "RECORDING.SOURCE_CHANGED", message: "Selected playback format or frame count differs from its validated source")
            }
            try verified.validateUnchanged()
            let graphSpan = RecordingDiagnosticSpan("playback.graph_prepare", context: diagnosticContext, sink: sink)
            do {
                rebuildGraph(format: opened.processingFormat)
                if mode == .offline {
                    try engine.enableManualRenderingMode(.offline, format: opened.processingFormat, maximumFrameCount: 4096)
                }
                graphSpan.finish()
            } catch { graphSpan.finish(.failure, error: error); throw error }
            file = opened; lease = verified; sourceRoot = source.assetRoot
            segmentStart = 0; segmentEnd = source.frameCount; pausedFrame = 0
            loading = nil
        } catch {
            if revision == loadRevision { loading = nil; file = nil; lease = nil; sourceRoot = nil }
            throw error
        }
    }

    public func play(from frame: Int64, through endFrame: Int64, generation: UInt64) throws {
        guard let file, let lease, let sourceRoot, frame >= 0, frame < endFrame, endFrame <= file.length else {
            throw failure("No verified playback segment is ready")
        }
        try Self.validateAssetRoot(sourceRoot)
        try lease.validateUnchanged()
        playing = false
        player.stop()
        self.generation = generation
        segmentStart = frame; segmentEnd = endFrame; pausedFrame = frame
        player.scheduleSegment(file, startingFrame: frame, frameCount: AVAudioFrameCount(endFrame - frame), at: nil, completionCallbackType: .dataPlayedBack) { [weak self] _ in
            Task { await self?.finished(generation: generation) }
        }
        do {
            if !engine.isRunning { engine.prepare(); try engine.start() }
            player.play()
            playing = true
            pausedFrame = nil
        } catch {
            player.stop(); engine.stop(); playing = false
            throw failure("The audio output could not start")
        }
    }

    public func pause() throws -> Int64? {
        let frame = position()
        player.pause()
        playing = false
        pausedFrame = frame
        return frame
    }

    public func position() -> Int64? {
        if let pausedFrame { return pausedFrame }
        guard graphReady else { return nil }
        guard let time = player.lastRenderTime, let clock = player.playerTime(forNodeTime: time), clock.isSampleTimeValid else { return nil }
        return min(segmentEnd, max(segmentStart, segmentStart + clock.sampleTime))
    }

    public func stop() throws {
        loadRevision &+= 1
        loading?.cancel(); loading = nil
        if let frame = position() { pausedFrame = frame }
        playing = false
        guard graphReady else { return }
        player.stop()
        engine.stop()
        guard !player.isPlaying, !engine.isRunning else { throw failure("Audio output did not stop; capture remains unavailable") }
    }

    public func recover() throws {
        try stop()
        observer = nil
        file = nil; lease = nil; sourceRoot = nil
        engine = AVAudioEngine(); player = AVAudioPlayerNode()
        graphReady = false
        pausedFrame = nil
    }

    /// Sleep/output-route signals end playback; they never select or start another source.
    public func interrupt(_ message: String) async {
        guard playing else { return }
        let token = generation
        do { try stop() }
        catch { await handler?(.failed(generation: token, error: failure("Audio output failed during route recovery"))); return }
        await handler?(.failed(generation: token, error: failure(message)))
    }

    /// Bounded real AVAudioEngine offline rendering for silent integration checks.
    public func renderOffline(frames: UInt32) throws -> [Float] {
        guard mode == .offline, (1...4096).contains(frames), engine.isInManualRenderingMode,
              let buffer = AVAudioPCMBuffer(pcmFormat: engine.manualRenderingFormat, frameCapacity: frames) else {
            throw failure("Offline rendering requires explicit offline mode and a bounded frame request")
        }
        let status = try engine.renderOffline(frames, to: buffer)
        guard status == .success, let samples = buffer.floatChannelData?[0] else { throw failure("Offline audio rendering did not produce the requested frames") }
        return Array(UnsafeBufferPointer(start: samples, count: Int(buffer.frameLength)))
    }

    private func rebuildGraph(format: AVAudioFormat) {
        observer = nil
        graphGeneration = UUID()
        let graph = graphGeneration
        engine = AVAudioEngine(); player = AVAudioPlayerNode()
        engine.attach(player)
        engine.connect(player, to: engine.mainMixerNode, format: format)
        graphReady = true
        observer = RecordingPlaybackNotificationLease(NotificationCenter.default.addObserver(forName: .AVAudioEngineConfigurationChange, object: engine, queue: nil) { [weak self] _ in
            Task { await self?.configurationChanged(graph: graph) }
        })
    }

    private func configurationChanged(graph: UUID) async {
        guard graph == graphGeneration else { return }
        await interrupt("Audio output configuration changed; review playback before continuing")
    }

    private func finished(generation: UInt64) async {
        guard generation == self.generation, playing else { return }
        playing = false
        pausedFrame = segmentEnd
        await handler?(.finished(generation: generation))
    }
    private func failure(_ message: String) -> RecordingContractError { RecordingContractError(code: "RECORDING.PLAYBACK_FAILED", message: message) }
    private func cancelled() -> RecordingContractError { RecordingContractError(code: "RECORDING.CANCELLED", message: "Playback source loading was superseded") }
    private static func validateAssetRoot(_ path: URL) throws {
        var root = stat()
        guard lstat(path.path, &root) == 0, root.st_mode & S_IFMT == S_IFDIR,
              root.st_uid == getuid(), root.st_mode & 0o077 == 0 else {
            throw RecordingContractError(code: "RECORDING.SOURCE_CHANGED", message: "Playback requires an owned private asset root")
        }
    }
}

private final class RecordingPlaybackNotificationLease: @unchecked Sendable {
    private let token: any NSObjectProtocol
    init(_ token: any NSObjectProtocol) { self.token = token }
    deinit { NotificationCenter.default.removeObserver(token) }
}
