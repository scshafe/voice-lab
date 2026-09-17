import CryptoKit
import Darwin
import Foundation
import RecordingContracts
import RecordingCore
import Testing

private actor Signal {
    private var opened = false
    private var waiters: [CheckedContinuation<Void, Never>] = []
    func wait() async { if !opened { await withCheckedContinuation { waiters.append($0) } } }
    func open() { opened = true; for waiter in waiters { waiter.resume() }; waiters.removeAll() }
}

private actor FakePlayback: RecordingPlaybackBackend {
    let entered = Signal()
    var release = Signal()
    var blockedTake: String?
    var generation: UInt64 = 0
    var currentFrame: Int64 = 0
    var plays: [(Int64, Int64, UInt64)] = []
    var stops = 0
    let stopEntered = Signal()
    var stopBarrier: Signal?
    var stopArmed = false
    var failStop = false
    var finishDuringStart = false
    var handler: (@Sendable (RecordingPlaybackBackendEvent) async -> Void)?
    init(blockedTake: String? = nil) { self.blockedTake = blockedTake }
    func setEventHandler(_ handler: @escaping @Sendable (RecordingPlaybackBackendEvent) async -> Void) { self.handler = handler }
    func load(_ source: RecordingPlaybackSource, generation: UInt64) async throws {
        self.generation = generation
        if source.takeID == blockedTake { release = Signal(); await entered.open(); await release.wait() }
        guard self.generation == generation else { throw RecordingContractError(code: "RECORDING.CANCELLED", message: "old load") }
        currentFrame = 0
    }
    func play(from frame: Int64, through endFrame: Int64, generation: UInt64) async {
        self.generation = generation; currentFrame = frame; plays.append((frame, endFrame, generation))
        if finishDuringStart { await handler?(.finished(generation: generation)) }
    }
    func pause() -> Int64? { currentFrame }
    func position() -> Int64? { currentFrame }
    func stop() async throws {
        stops += 1
        if failStop { throw RecordingContractError(code: "RECORDING.PLAYBACK_FAILED", message: "injected output shutdown failure") }
        // Only the first stop after holdStop() blocks, so a racing transport operation
        // reaches its own backend calls instead of deadlocking on this barrier.
        if stopArmed, let stopBarrier { stopArmed = false; await stopEntered.open(); await stopBarrier.wait() }
        generation &+= 1
        await release.open()
    }
    func holdStop() { stopBarrier = Signal(); stopArmed = true }
    func releaseStop() async { await stopBarrier?.open(); stopBarrier = nil }
    func recover() { failStop = false }
    func setFrame(_ frame: Int64) { currentFrame = frame }
    func setStopFailure(_ enabled: Bool) { failStop = enabled }
    func setFinishDuringStart() { finishDuringStart = true }
    func finished(_ token: UInt64) async { await handler?(.finished(generation: token)) }
    func failed(_ token: UInt64) async { await handler?(.failed(generation: token, error: RecordingContractError(code: "RECORDING.PLAYBACK_FAILED", message: "late route callback"))) }
}

private func source(_ id: String = "take", frames: Int64 = 4800) throws -> RecordingPlaybackSource {
    try RecordingPlaybackSource(takeID: id, sourceSHA256: String(repeating: "a", count: 64), assetRoot: URL(fileURLWithPath: "/private/tmp/synthetic-playback"), relativePath: "tmp/\(id).wav", frameCount: frames)
}

/// A private asset root holding one mono PCM WAV written in `format`, and the source that
/// declares it. `declared` lets a test declare the file as another format.
private struct AudioFixture {
    let root: URL
    let source: RecordingPlaybackSource
    let bytes: Data
    let samples: [Int32]
    /// Full scale of one sample in the written format.
    let fullScale: Float
    init(frames: Int = 4800, format: RecordingPlaybackFormat = .capture, declared: RecordingPlaybackFormat? = nil) throws {
        let temporary = try #require(realpath(FileManager.default.temporaryDirectory.path, nil))
        defer { free(temporary) }
        root = URL(fileURLWithPath: String(cString: temporary), isDirectory: true).appendingPathComponent("cole-playback-\(UUID().uuidString.lowercased())", isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        let width = Int(format.bytesPerFrame)
        let (step, offset): (Int, Int) = format == .capture ? (4000, 252_000) : (500, 31_500)
        samples = (0..<frames).map { (index: Int) -> Int32 in Int32((index % 127) * step - offset) }
        fullScale = format == .capture ? 8_388_608 : 32_768
        var wav = Data("RIFF".utf8)
        func u32(_ value: UInt32) { var little = value.littleEndian; withUnsafeBytes(of: &little) { wav.append(contentsOf: $0) } }
        func u16(_ value: UInt16) { var little = value.littleEndian; withUnsafeBytes(of: &little) { wav.append(contentsOf: $0) } }
        let rate = UInt32(format.sampleRate)
        u32(UInt32(36 + frames * width)); wav.append(Data("WAVEfmt ".utf8)); u32(16)
        u16(1); u16(1); u32(rate); u32(rate * UInt32(width)); u16(UInt16(width)); u16(UInt16(format.bitsPerSample))
        wav.append(Data("data".utf8)); u32(UInt32(frames * width))
        var pcm = [UInt8](); pcm.reserveCapacity(frames * width)
        for sample in samples { for byte in 0..<width { pcm.append(UInt8(truncatingIfNeeded: sample >> (8 * byte))) } }
        wav.append(contentsOf: pcm)
        bytes = wav
        let file = root.appendingPathComponent("original.wav")
        try wav.write(to: file, options: .withoutOverwriting)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        let digest = SHA256.hash(data: wav).map { String(format: "%02x", $0) }.joined()
        source = try RecordingPlaybackSource(takeID: "synthetic", sourceSHA256: digest, assetRoot: root, relativePath: "original.wav",
                                             frameCount: Int64(frames), format: declared ?? format)
    }
    func remove() throws { try FileManager.default.removeItem(at: root) }
}

@Suite(.serialized) struct RecordingPlaybackTests {
    @Test @MainActor func selectionSeekAndTrimNeverAutoplayAndRememberEachTakeCursor() async throws {
        let backend = FakePlayback()
        let actual = RecordingPlaybackController(backend: backend)
        try await actual.select(source("a"), retainedRange: 10..<4000)
        try await actual.seek(to: 1234)
        try await actual.previewRange(20..<3500)
        try await actual.select(source("b"), retainedRange: 0..<4800)
        try await actual.seek(to: 2000)
        try await actual.select(source("a"), retainedRange: 20..<3500)
        #expect(actual.cursorFrame == 1234)
        #expect(actual.phase == .stopped)
        #expect(await backend.plays.isEmpty)
        try await actual.seek(to: .max)
        #expect(actual.cursorFrame == 3500)
        try await actual.skip(frames: .min)
        #expect(actual.cursorFrame == 20)
    }

    @Test @MainActor func backendFrameClockDrivesCursorAndLateCompletionCannotReplaceNewSelection() async throws {
        let backend = FakePlayback()
        let actual = RecordingPlaybackController(backend: backend)
        try await actual.select(source("a"), retainedRange: 100..<4000)
        try await actual.playPause()
        let old = actual.generation
        await backend.setFrame(777)
        await actual.refreshPosition()
        #expect(actual.cursorFrame == 777)
        try await actual.seek(to: 2000)
        await backend.finished(old)
        #expect(actual.phase == .playing && actual.cursorFrame == 2000)
        try await actual.select(source("b"), retainedRange: 0..<4800)
        await backend.failed(old)
        #expect(actual.source?.takeID == "b" && actual.failure == nil && actual.phase == .stopped)
        try await actual.playPause()
        await backend.finished(actual.generation)
        #expect(actual.cursorFrame == 4800 && actual.phase == .stopped)
    }

    @Test @MainActor func currentGenerationOutputFailureStopsTransportAndBlocksRecordUntilRecovery() async throws {
        let backend = FakePlayback()
        let actual = RecordingPlaybackController(backend: backend)
        try await actual.select(source("a"), retainedRange: 0..<4800)
        try await actual.playPause()
        #expect(actual.phase == .playing && actual.failure == nil && actual.canPlay)
        // An asynchronous route/interruption failure for the selected generation is surfaced,
        // unlike the superseded token in backendFrameClockDrives..., which stays ignored.
        await backend.failed(actual.generation)
        #expect(actual.phase == .failed && actual.failure?.code == "RECORDING.PLAYBACK_FAILED")
        #expect(!actual.canPlay)
        await #expect(throws: RecordingContractError.self) { try await actual.playPause() }
        await #expect(throws: RecordingContractError.self) { try await actual.beginCapture() }
        #expect(!actual.captureOwned)
        try await actual.recover()
        #expect(actual.failure == nil && actual.phase == .stopped && actual.cursorFrame == 0)
        try await actual.playPause()
        #expect(actual.phase == .playing)
        #expect(await backend.plays.count == 2)
    }

    @Test @MainActor func failedOutputShutdownBlocksCaptureUntilExplicitRecovery() async throws {
        let backend = FakePlayback()
        let controller = RecordingPlaybackController(backend: backend)
        try await controller.select(source(), retainedRange: 0..<4800)
        try await controller.playPause()
        await backend.setStopFailure(true)
        await #expect(throws: RecordingContractError.self) { try await controller.beginCapture() }
        #expect(!controller.captureOwned && controller.failure?.code == "RECORDING.PLAYBACK_FAILED")
        await backend.setStopFailure(false)
        await #expect(throws: RecordingContractError.self) { try await controller.beginCapture() }
        try await controller.recover()
        try await controller.beginCapture()
        #expect(controller.captureOwned && !controller.canPlay)
        await #expect(throws: RecordingContractError.self) { try await controller.playPause() }
        controller.endCapture()
        #expect(controller.canPlay)
        #expect(await backend.plays.count == 1)
    }

    @Test @MainActor func captureCancelsLoadingWithoutWaitingForSelectedAudio() async throws {
        let backend = FakePlayback(blockedTake: "slow")
        let controller = RecordingPlaybackController(backend: backend)
        let task = Task { try await controller.select(source("slow"), retainedRange: 0..<4800) }
        await backend.entered.wait()
        try await controller.beginCapture()
        await #expect(throws: RecordingContractError.self) { try await task.value }
        #expect(controller.captureOwned && controller.source == nil && controller.failure == nil)
        #expect(await backend.plays.isEmpty)
    }

    @Test @MainActor func shutdownHoldsTheTransportClosedWhileOutputIsStopping() async throws {
        let backend = FakePlayback()
        let controller = RecordingPlaybackController(backend: backend)
        try await controller.select(source(), retainedRange: 0..<4800)
        try await controller.playPause()
        await backend.holdStop()
        let closing = Task { try await controller.shutdown() }
        await backend.stopEntered.wait()
        // A key, menu or waveform action delivered while the output is stopping cannot
        // start audio that outlives the close. Stopping-only work stays available.
        await #expect(throws: RecordingContractError.self) { try await controller.playPause() }
        await #expect(throws: RecordingContractError.self) { try await controller.seek(to: 400) }
        await #expect(throws: RecordingContractError.self) { try await controller.skip(frames: 100) }
        await backend.releaseStop()
        try await closing.value
        #expect(controller.phase == .stopped)
        #expect(await backend.plays.count == 1)
        try await controller.playPause()
        #expect(controller.phase == .playing)
    }

    @Test func actualOfflineEngineRendersOnlyExactRetainedSourceFrames() async throws {
        let fixture = try AudioFixture()
        defer { try? fixture.remove() }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        try await backend.load(fixture.source, generation: 1)
        try await backend.play(from: 123, through: 2123, generation: 2)
        var rendered: [Float] = []
        var playhead: [Int64] = []
        while rendered.count < 2000 {
            rendered.append(contentsOf: try await backend.renderOffline(frames: UInt32(min(512, 2000 - rendered.count))))
            playhead.append(try #require(await backend.position()))
        }
        #expect(rendered.count == 2000)
        for (index, sample) in rendered.enumerated() { #expect(abs(sample - Float(fixture.samples[123 + index]) / fixture.fullScale) < 0.000001) }
        // The playhead follows the actual player clock from the segment start. The three
        // interior probes are strictly inside the retained range, so a frozen clock or a
        // playhead that only reports its clamp cannot satisfy them.
        #expect(playhead == [635, 1147, 1659, 2123])
        try await backend.stop()
        #expect(try Data(contentsOf: fixture.root.appendingPathComponent("original.wav")) == fixture.bytes)
    }

    @Test @MainActor func pauseStopsOnlyPlayingOutputAndKeepsItsPosition() async throws {
        let backend = FakePlayback()
        let controller = RecordingPlaybackController(backend: backend)
        // Nothing selected, then a stopped source: pausing changes nothing.
        try await controller.pause()
        #expect(controller.phase == .empty)
        try await controller.select(source(), retainedRange: 0..<4800)
        let stopped = controller.generation
        try await controller.pause()
        #expect(controller.phase == .stopped && controller.generation == stopped)
        try await controller.playPause()
        await backend.setFrame(1_500)
        try await controller.pause()
        #expect(controller.phase == .paused && controller.cursorFrame == 1_500 && controller.canPlay)
        let paused = controller.generation
        try await controller.pause()
        #expect(controller.generation == paused, "a paused transport is left as it is")
        // Playing again resumes where it paused.
        try await controller.playPause()
        #expect(controller.phase == .playing)
        #expect(await backend.plays.map(\.0) == [0, 1_500])
    }

    @Test @MainActor func completionBeforeStartReplyDoesNotLeaveTransportPlaying() async throws {
        let backend = FakePlayback()
        let controller = RecordingPlaybackController(backend: backend)
        try await controller.select(source(), retainedRange: 123..<124)
        await backend.setFinishDuringStart()
        try await controller.playPause()
        #expect(controller.phase == .stopped && controller.cursorFrame == 124)
    }

    @Test @MainActor func supersededSlowSelectionKeepsLatestSourceAndCursorMemoryIsBounded() async throws {
        let backend = FakePlayback(blockedTake: "slow")
        let controller = RecordingPlaybackController(backend: backend)
        try await controller.select(source("original"), retainedRange: 0..<4800)
        try await controller.seek(to: 200)
        let slow = Task { try await controller.select(source("slow"), retainedRange: 0..<4800) }
        await backend.entered.wait()
        #expect(controller.source?.takeID == "original" && controller.cursorFrame == 200)
        try await controller.select(source("latest"), retainedRange: 0..<4800)
        await backend.release.open()
        await #expect(throws: RecordingContractError.self) { try await slow.value }
        #expect(controller.source?.takeID == "latest" && controller.failure == nil && controller.canPlay)
        for number in 0..<250 {
            try await controller.select(source("history-\(number)"), retainedRange: 0..<4800)
            try await controller.seek(to: 1000)
        }
        try await controller.select(source("original"), retainedRange: 0..<4800)
        #expect(controller.cursorFrame == 0)
        try await controller.select(source("history-249"), retainedRange: 0..<4800)
        #expect(controller.cursorFrame == 1000)
        #expect(await backend.plays.isEmpty)
    }

    @Test func positionIsUnknownRatherThanFatalBeforeTheFirstRenderCycle() async throws {
        let fixture = try AudioFixture(frames: 2400, format: .speech)
        defer { try? fixture.remove() }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        try await backend.load(fixture.source, generation: 1)
        try await backend.play(from: 0, through: 2400, generation: 2)
        // A view polling the position between play() and the first rendered frames.
        let unknown = await backend.position()
        #expect(unknown == nil || unknown == 0, "\(String(describing: unknown))")
        _ = try await backend.renderOffline(frames: 480)
        #expect(await backend.position() == 480)
        try await backend.stop()
    }

    @Test func idleBackendShutdownAndInvalidRangesAreSafeWithoutAnAudioGraph() async throws {
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        #expect(await backend.position() == nil)
        try await backend.stop()
        try await backend.recover()
        await #expect(throws: RecordingContractError.self) { try await backend.play(from: 0, through: 1, generation: 1) }
        await #expect(throws: RecordingContractError.self) { try await backend.renderOffline(frames: 4097) }
    }

    @Test func selectedSourceMutationWithPreservedMtimeBlocksPlaybackBeforeOutput() async throws {
        let fixture = try AudioFixture()
        defer { try? fixture.remove() }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        try await backend.load(fixture.source, generation: 1)
        let path = fixture.root.appendingPathComponent("original.wav")
        let attributes = try FileManager.default.attributesOfItem(atPath: path.path)
        var changed = fixture.bytes; changed[44] ^= 1
        try changed.write(to: path)
        try FileManager.default.setAttributes([.modificationDate: attributes[.modificationDate]!], ofItemAtPath: path.path)
        await #expect(throws: RecordingContractError.self) { try await backend.play(from: 0, through: 4800, generation: 2) }
        try await backend.stop()
    }

    @Test func selectedChecksumFormatAndPrivatePathsFailClosed() async throws {
        let fixture = try AudioFixture()
        defer { try? fixture.remove() }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        let wrong = try RecordingPlaybackSource(takeID: "wrong", sourceSHA256: String(repeating: "0", count: 64), assetRoot: fixture.root, relativePath: "original.wav", frameCount: 4800)
        await #expect(throws: RecordingContractError.self) { try await backend.load(wrong, generation: 1) }
        let alias = fixture.root.appendingPathComponent("alias.wav")
        try FileManager.default.createSymbolicLink(at: alias, withDestinationURL: fixture.root.appendingPathComponent("original.wav"))
        let linked = try RecordingPlaybackSource(takeID: "alias", sourceSHA256: fixture.source.sourceSHA256, assetRoot: fixture.root, relativePath: "alias.wav", frameCount: 4800)
        await #expect(throws: RecordingContractError.self) { try await backend.load(linked, generation: 2) }
        #expect(throws: RecordingContractError.self) { try RecordingPlaybackSource(takeID: "bad", sourceSHA256: fixture.source.sourceSHA256, assetRoot: fixture.root, relativePath: "../original.wav", frameCount: 4800) }
        try await backend.stop()
    }

    // MARK: declared formats

    @Test func aSourceDeclaresCaptureUnlessItNamesSpeech() throws {
        #expect(RecordingPlaybackFormat.capture.sampleRate == 48_000 && RecordingPlaybackFormat.capture.bitsPerSample == 24)
        #expect(RecordingPlaybackFormat.capture.channels == 1 && RecordingPlaybackFormat.capture.bytesPerFrame == 3)
        #expect(RecordingPlaybackFormat.speech.sampleRate == 24_000 && RecordingPlaybackFormat.speech.bitsPerSample == 16)
        #expect(RecordingPlaybackFormat.speech.channels == 1 && RecordingPlaybackFormat.speech.bytesPerFrame == 2)
        #expect(RecordingPlaybackFormat.capture.maximumFileBytes(frameCount: 4800) == 14_400 + 1_048_576)
        #expect(RecordingPlaybackFormat.speech.maximumFileBytes(frameCount: 4800) == 9_600 + 1_048_576)
        #expect(try source().format == .capture, "existing callers keep the capture format")
        let speech = try RecordingPlaybackSource(takeID: "speak-one", sourceSHA256: String(repeating: "a", count: 64),
            assetRoot: URL(fileURLWithPath: "/private/tmp/synthetic-playback"), relativePath: "derived/speech/speak-one/speech-01.wav",
            frameCount: 24_000, format: .speech)
        let undeclared = try RecordingPlaybackSource(takeID: "speak-one", sourceSHA256: speech.sourceSHA256,
            assetRoot: speech.assetRoot, relativePath: speech.relativePath, frameCount: 24_000)
        #expect(speech.format == .speech && undeclared.format == .capture && speech != undeclared)
    }

    @Test func actualOfflineEngineRendersASpeechClipAtItsOwnRate() async throws {
        let fixture = try AudioFixture(frames: 2400, format: .speech)
        defer { try? fixture.remove() }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        try await backend.load(fixture.source, generation: 1)
        try await backend.play(from: 100, through: 1100, generation: 2)
        var rendered: [Float] = []
        while rendered.count < 1000 {
            rendered.append(contentsOf: try await backend.renderOffline(frames: UInt32(min(512, 1000 - rendered.count))))
        }
        #expect(rendered.count == 1000)
        for (index, sample) in rendered.enumerated() { #expect(abs(sample - Float(fixture.samples[100 + index]) / fixture.fullScale) < 0.000001) }
        #expect(await backend.position() == 1100)
        try await backend.stop()
        #expect(try Data(contentsOf: fixture.root.appendingPathComponent("original.wav")) == fixture.bytes)
    }

    @Test(arguments: [(RecordingPlaybackFormat.speech, RecordingPlaybackFormat.capture), (.capture, .speech)])
    func aFileOfTheOtherFormatIsRefused(written: RecordingPlaybackFormat, declared: RecordingPlaybackFormat) async throws {
        let fixture = try AudioFixture(frames: 4800, format: written, declared: declared)
        defer { try? fixture.remove() }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        do {
            try await backend.load(fixture.source, generation: 1)
            Issue.record("A \(written) file declared as \(declared) was accepted")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.SOURCE_CHANGED")
            #expect(error.message == "Selected playback format or frame count differs from its validated source", "\(error)")
        }
        await #expect(throws: RecordingContractError.self) { try await backend.play(from: 0, through: 4800, generation: 2) }
        // The same file declared as what it is plays.
        let honest = try RecordingPlaybackSource(takeID: "synthetic", sourceSHA256: fixture.source.sourceSHA256, assetRoot: fixture.root,
                                                 relativePath: "original.wav", frameCount: 4800, format: written)
        try await backend.load(honest, generation: 3)
        try await backend.stop()
    }

    @Test func theFileSizeBoundFollowsTheDeclaredFormat() async throws {
        // 1,100,000 capture frames are 3,300,044 bytes: inside the capture bound (3,300,000 + 1 MiB)
        // and past the speech bound for the same frame count (2,200,000 + 1 MiB).
        let fixture = try AudioFixture(frames: 1_100_000, format: .capture, declared: .speech)
        defer { try? fixture.remove() }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        do {
            try await backend.load(fixture.source, generation: 1)
            Issue.record("A file past the speech size bound was opened")
        } catch let error as RecordingContractError {
            #expect(error.code == "RECORDING.SOURCE_CHANGED" && error.context["boundary"] == "input-identity", "\(error)")
        }
        let capture = try RecordingPlaybackSource(takeID: "synthetic", sourceSHA256: fixture.source.sourceSHA256, assetRoot: fixture.root,
                                                  relativePath: "original.wav", frameCount: 1_100_000)
        try await backend.load(capture, generation: 2)
        try await backend.stop()
    }
}
