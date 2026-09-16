import AudioToolbox
import CaptureBuffer
import CryptoKit
import Darwin
import Foundation
@testable import RecordingCore
import RecordingContracts
import Testing

private final class TestClock: @unchecked Sendable {
    private let lock = NSLock()
    private var elapsed = 0.0
    let base = Date(timeIntervalSince1970: 1_789_084_800)
    var clock: NativeCaptureClock {
        NativeCaptureClock(now: { [self] in lock.lock(); defer { lock.unlock() }; return base.addingTimeInterval(elapsed) },
                           monotonicSeconds: { [self] in lock.lock(); defer { lock.unlock() }; return elapsed },
                           hostTicks: { [self] in lock.lock(); defer { lock.unlock() }; return 1_000_000 + UInt64(elapsed * 1_000_000) },
                           hostSecondsPerTick: 0.000001)
    }
    func advance(_ seconds: Double) { lock.lock(); elapsed += seconds; lock.unlock() }
}

/// Wall and nanosecond host clocks advance independently, like a slewed or stepped
/// wall clock beside mach_absolute_time. TestClock deliberately couples them.
private final class SkewClock: @unchecked Sendable {
    private let lock = NSLock()
    private var wall = 0.0
    private var host: UInt64 = 5_000_000_000
    let base = Date(timeIntervalSince1970: 1_789_084_800)
    var clock: NativeCaptureClock {
        NativeCaptureClock(now: { [self] in lock.lock(); defer { lock.unlock() }; return base.addingTimeInterval(wall) },
                           monotonicSeconds: { [self] in lock.lock(); defer { lock.unlock() }; return Double(host) / 1_000_000_000 },
                           hostTicks: { [self] in lock.lock(); defer { lock.unlock() }; return host },
                           hostSecondsPerTick: 0.000_000_001)
    }
    func advance(hostNanoseconds: UInt64, wallSeconds: Double) {
        lock.lock(); host += hostNanoseconds; wall += wallSeconds; lock.unlock()
    }
}

/// One-second host-stamped take: each 480-frame callback is stamped at host now,
/// then the wall clock moves `wallRate` seconds per host second.
private func skewedTake(_ fixture: CaptureFixture, _ skew: SkewClock, name: String, wallRate: Double,
                        chunks: Int = 100, futureHostNanoseconds: UInt64 = 0, requestedAtText: String? = nil,
                        writeHook: @escaping NativeCaptureWriteHook = { _ in }) throws -> (NativeCaptureOutcome, NativeCaptureRequest) {
    let backend = SyntheticCaptureBackend(clock: skew.clock)
    let controller = NativeCaptureController(backendFactory: { backend }, clock: skew.clock, writeHook: writeHook)
    let request = try fixture.request(name, requestedAtText: requestedAtText ?? NativeCaptureTime.string(skew.base))
    try controller.start(request: request)
    try backend.waitUntilStarted()
    for index in 0..<chunks {
        try backend.emit(pcm: pcm(480, start: index * 480), sampleTime: Double(index * 480),
                         hostTime: skew.clock.hostTicks() + futureHostNanoseconds)
        skew.advance(hostNanoseconds: 10_000_000, wallSeconds: 0.01 * wallRate)
        controller.waitForWriterBarrier()
    }
    controller.requestStop()
    return (try controller.finish(), request)
}

/// The same parsed ordering the Swift and Python seal validators require.
private func sealTimestamps(_ seal: [String: RecordingJSON]) throws -> [Date] {
    let dates = try ["requested_at", "first_sample_at", "last_sample_at", "sealed_at"].map { key in
        try #require(seal[key]?.string.flatMap(NativeCaptureTime.parse))
    }
    #expect(zip(dates, dates.dropFirst()).allSatisfy { $0 <= $1 }, "Persisted capture timestamps are not ordered")
    return dates
}

private func failureDetail(_ failure: NativeCaptureFailure?, _ key: String) -> String? {
    failure?.details.first { $0.hasPrefix(key + "=") }.map { String($0.dropFirst(key.count + 1)) }
}

private final class TestBarrier: @unchecked Sendable {
    private let entered = DispatchSemaphore(value: 0)
    private let proceed = DispatchSemaphore(value: 0)
    func hold() { entered.signal(); proceed.wait() }
    func wait() throws { guard entered.wait(timeout: .now() + 5) == .success else { throw NativeCaptureFailure("RECORDING.WRITE_FAILED", "Test barrier timed out") } }
    func release() { proceed.signal() }
}

private struct CaptureFixture {
    let root: URL
    let clock = TestClock()
    init() throws {
        guard let resolved = realpath(FileManager.default.temporaryDirectory.path, nil) else {
            throw NativeCaptureFailure.system("resolve private synthetic directory")
        }
        defer { free(resolved) }
        root = URL(fileURLWithPath: String(cString: resolved))
            .appendingPathComponent("cole-native-capture-test-\(UUID().uuidString.lowercased())")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false,
                                               attributes: [.posixPermissions: 0o700])
    }
    func request(_ name: String = "synthetic-take", maximum: UInt64 = 28_800_000,
                 requestedAtText: String? = nil) throws -> NativeCaptureRequest {
        let directory = root.appendingPathComponent("tmp/recording-takes/\(name)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                               attributes: [.posixPermissions: 0o700])
        let time = requestedAtText ?? NativeCaptureTime.string(clock.base)
        let prompt: RecordingJSON = .object(["prompt_id": .string("synthetic-prompt"), "text": .string("Synthetic instructions"),
                                             "category": .string("neutral"), "style": .string("Synthetic"), "expected_seconds": .number(1)])
        let reservation: RecordingJSON = .object([
            "schema_version": .string("cole-voice-lab.capture-reservation/v1"),
            "take_id": .string(name), "operation_id": .string("synthetic-operation-\(name)"),
            "process_generation": .string("synthetic-generation"),
            "requested_at": .string(time), "prompt": prompt,
            "partial_path": .string("tmp/recording-takes/\(name)/capture.partial.wav"),
            "original_path": .string("tmp/recording-takes/\(name)/original.wav"),
            "maximum_frames": .integer(Int64(maximum)),
            "group": .object(["capture_device": .object([
                "backend": .string("coreaudio"), "device_id": .string("synthetic-umik"),
                "display_name": .string("Synthetic UMIK-1"), "microphone_serial": .null,
            ])]),
        ])
        return try NativeCaptureRequest(reservation: reservation, reservationSHA256: String(repeating: "a", count: 64),
            recorderBuild: .object(["build_id": .string("synthetic-build"),
                                    "source_sha256": .string(String(repeating: "b", count: 64)),
                                    "executable_sha256": .string(String(repeating: "c", count: 64)),
                                    "environment_sha256": .string(String(repeating: "d", count: 64))]),
            assetRoot: root, bufferGeneration: 7)
    }
    func cleanup() { try? FileManager.default.removeItem(at: root) }
}

private func pcm(_ frames: Int, start: Int = 0) -> Data {
    var data = Data()
    for frame in start..<(start + frames) {
        let sample = frame % 2 == 0 ? 2_000_000 : -2_000_000
        for shift in [0, 8, 16] { data.append(UInt8(truncatingIfNeeded: sample >> shift)) }
    }
    return data
}

@Suite(.serialized) struct NativeCaptureTests {
    @Test func testPythonMicrosecondTimestampsRemainExactInNativeEvidence() throws {
        let text = "2026-09-11T00:00:00.123456Z"
        let date = try #require(NativeCaptureTime.parse(text))
        #expect(NativeCaptureTime.string(date) == text)
        #expect(NativeCaptureTime.parse("2026-09-11T00:00:00Z") != nil)
        #expect(NativeCaptureTime.parse("not-a-time") == nil)
    }
    @Test func testExactPCMSealsDurablyAndControllerCanRecordIndependentNextTake() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        fixture.clock.advance(0.001)
        let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
        let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock)
        for name in ["first-take", "second-take"] {
            let requestedText = NativeCaptureTime.string(fixture.clock.base)
                .replacingOccurrences(of: ".000000Z", with: ".000123Z")
            let request = try fixture.request(name, requestedAtText: requestedText)
            try controller.start(request: request)
            try backend.waitUntilStarted()
            let host = fixture.clock.clock.hostTicks()
            try backend.emit(pcm: pcm(480), sampleTime: 0, hostTime: host)
            fixture.clock.advance(0.01)
            controller.waitForWriterBarrier()
            #expect(controller.progress()?.writtenFrames == 480)
            controller.requestStop()
            let outcome = try controller.finish()
            #expect(outcome.failure == nil && outcome.recoveryPersisted)
            let seal = try #require(outcome.seal?.object)
            #expect(seal["requested_at"]?.string == request.requestedAtText)
            let file = fixture.root.appendingPathComponent(request.originalPath)
            let bytes = try Data(contentsOf: file)
            #expect(bytes.count == 44 + 1440)
            #expect(bytes.suffix(1440) == pcm(480))
            #expect(seal["original_sha256"]?.string == SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined())
            #expect(seal["audio"]?.object?["frames"]?.integer == 480)
            #expect(try controller.waveformSnapshot()?.writtenFrames == 480)
            let sourceDigest = try #require(seal["original_sha256"]?.string)
            let archive = try controller.waveformArchive(takeID: name, sourceSHA256: sourceDigest, finalWrittenFrames: 480)
            let failedCache = RecordingWaveformArchiveStore.persist(archive, sourceSHA256: sourceDigest, assetRoot: fixture.root,
                writeHook: { throw NativeCaptureFailure("RECORDING.WRITE_FAILED", "Synthetic derived cache failure") })
            guard case .needsRebuild = failedCache else { Issue.record("Injected cache failure was hidden"); return }
            #expect(try controller.finish().seal == outcome.seal)
            #expect(try Data(contentsOf: file) == bytes)
            let persistedCache = RecordingWaveformArchiveStore.persist(archive, sourceSHA256: sourceDigest, assetRoot: fixture.root)
            guard case .stored = persistedCache else { Issue.record("Derived cache did not persist"); return }
            let historical = try RecordingWaveformArchiveStore.load(sourceSHA256: sourceDigest, assetRoot: fixture.root, takeID: name, processGeneration: "historical")
            #expect(try historical.snapshot().writtenFrames == 480 && historical.snapshot().sampleClock == nil)
            #expect(!FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent(request.partialPath).path))
            let saved = try RecordingJSON.parse(Data(contentsOf: file.deletingLastPathComponent().appendingPathComponent("native-recovery.json")))
            #expect(saved == outcome.recovery)
            if let evidenceRoot = ProcessInfo.processInfo.environment["COLE_VOICE_CAPTURE_TEST_EVIDENCE_ROOT"] {
                let evidence = URL(fileURLWithPath: evidenceRoot).appendingPathComponent(name)
                try FileManager.default.createDirectory(at: evidence, withIntermediateDirectories: false,
                                                       attributes: [.posixPermissions: 0o700])
                try bytes.write(to: evidence.appendingPathComponent("original.wav"))
                try outcome.recovery.transportData().write(to: evidence.appendingPathComponent("native-recovery.json"))
                for file in ["original.wav", "native-recovery.json"] {
                    try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: evidence.appendingPathComponent(file).path)
                }
            }
            #expect(backend.isDisposed)
        }
        #expect(backend.openedCount == 2 && backend.disposedCount == 2)
    }

    @Test func testStopDrainsFinalPCMAndNoAutomaticNextRecording() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
        let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock)
        try controller.start(request: fixture.request())
        try backend.waitUntilStarted()
        try backend.emit(pcm: pcm(480), sampleTime: 0, hostTime: fixture.clock.clock.hostTicks())
        controller.waitForWriterBarrier()
        let beforeStopFrames = try #require(try controller.waveformSnapshot()?.writtenFrames)
        #expect(beforeStopFrames == 480)
        backend.setStopDrainPCM(pcm(120, start: 480))
        fixture.clock.advance(0.02)
        controller.requestStop()
        let outcome = try controller.finish()
        #expect(outcome.failure == nil)
        #expect(outcome.seal?.object?["audio"]?.object?["frames"]?.integer == 600)
        // Stop drains complete callback frames that the live timer has not observed.
        // The final written snapshot must agree with the seal, not the pre-Stop one.
        #expect(try controller.waveformSnapshot()?.writtenFrames == 600)
        #expect(backend.openedCount == 1 && backend.disposedCount == 1)
    }

    @Test func testStopReachesControllerWhileWriterIsBlocked() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let barrier = TestBarrier()
        let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
        let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock,
            writeHook: { point in if case .pcm = point { barrier.hold() } })
        try controller.start(request: fixture.request())
        try backend.waitUntilStarted()
        try backend.emit(pcm: pcm(480), sampleTime: 0, hostTime: fixture.clock.clock.hostTicks())
        try barrier.wait()
        fixture.clock.advance(0.01)
        controller.requestStop()
        controller.waitForControlBarrier()
        #expect(backend.isDisposed && controller.progress()?.stopRequested == true)
        #expect(controller.progress()?.writtenFrames == 0)
        barrier.release()
        #expect(try controller.finish().failure == nil)
    }

    @Test func testStopDuringStartupDoesNotOpenAnotherTakeOrCreateCompletedWAV() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let barrier = TestBarrier()
        let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
        backend.setStartHook { barrier.hold() }
        let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock)
        let request = try fixture.request()
        try controller.start(request: request)
        try barrier.wait()
        controller.requestStop()
        #expect(controller.progress()?.stopRequested == true)
        barrier.release()
        let outcome = try controller.finish()
        #expect(outcome.failure?.code == "RECORDING.CANCELLED")
        #expect(outcome.seal == nil && outcome.recoveryPersisted)
        #expect(FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent(request.partialPath).path))
        #expect(!FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent(request.originalPath).path))
    }

    @Test func testGapAndDeviceChangePreservePartialWithoutSuccessfulSeal() throws {
        for failureKind in ["gap", "device"] {
            let fixture = try CaptureFixture(); defer { fixture.cleanup() }
            let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
            let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock)
            try controller.start(request: fixture.request())
            try backend.waitUntilStarted()
            try backend.emit(pcm: pcm(480), sampleTime: 0, hostTime: fixture.clock.clock.hostTicks())
            fixture.clock.advance(0.02)
            if failureKind == "gap" {
                #expect(throws: NativeCaptureFailure.self) {
                    try backend.emit(pcm: pcm(480), sampleTime: 481, hostTime: fixture.clock.clock.hostTicks())
                }
            } else {
                backend.injectHealthFailure(NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Synthetic format changed"))
            }
            controller.checkNow()
            let outcome = try controller.finish()
            #expect(outcome.seal == nil && outcome.recoveryPersisted)
            #expect(outcome.failure?.code == (failureKind == "gap" ? "RECORDING.CONTINUITY_FAILED" : "RECORDING.DEVICE_UNAVAILABLE"))
        }
    }

    @Test func testDiskAndSyncFailuresNeverAcknowledgeSuccessfulSeal() throws {
        for point in [NativeCaptureWritePoint.pcm, .fileSync, .directorySync, .recoverySync] {
            let fixture = try CaptureFixture(); defer { fixture.cleanup() }
            let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
            let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock,
                writeHook: { current in
                    if String(describing: current) == String(describing: point) {
                        throw NativeCaptureFailure("RECORDING.WRITE_FAILED", "Synthetic disk fault")
                    }
                })
            try controller.start(request: fixture.request())
            try backend.waitUntilStarted()
            try backend.emit(pcm: pcm(480), sampleTime: 0, hostTime: fixture.clock.clock.hostTicks())
            fixture.clock.advance(0.02)
            controller.requestStop()
            let outcome = try controller.finish()
            #expect(outcome.seal == nil && outcome.failure?.code == "RECORDING.WRITE_FAILED")
        }
    }

    @Test func testMaximumFramesAndMonotonicStartupDeadlineAreIndependent() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
        let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock)
        try controller.start(request: fixture.request("maximum-take", maximum: 480))
        try backend.waitUntilStarted()
        try backend.emit(pcm: pcm(600), sampleTime: 0, hostTime: fixture.clock.clock.hostTicks())
        fixture.clock.advance(0.02)
        controller.checkNow()
        let outcome = try controller.finish()
        #expect(outcome.seal?.object?["audio"]?.object?["frames"]?.integer == 480)
        #expect(outcome.seal?.object?["continuity"]?.object?["stop_reason"]?.string == "maximum-duration")
        try controller.start(request: fixture.request("startup-timeout"))
        try backend.waitUntilStarted()
        fixture.clock.advance(11)
        controller.checkNow()
        #expect(try controller.finish().failure?.code == "RECORDING.DEVICE_UNAVAILABLE")
    }

    @Test func testMaximumWallDeadlineIncludesStartupAndPreservesEveryReceivedFrameWithoutPadding() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
        backend.setStartHook { fixture.clock.advance(0.25) }
        let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock)
        let request = try fixture.request("wall-deadline-short-take", maximum: 48_000)
        try controller.start(request: request)
        try backend.waitUntilStarted()
        controller.waitForControlBarrier()
        try backend.emit(pcm: pcm(480), sampleTime: 0, hostTime: fixture.clock.clock.hostTicks())
        fixture.clock.advance(0.74)
        controller.checkNow()
        #expect(controller.progress()?.stopRequested == false)
        fixture.clock.advance(0.01)
        controller.checkNow()
        let outcome = try controller.finish()
        #expect(outcome.failure == nil && outcome.recoveryPersisted)
        let seal = try #require(outcome.seal?.object)
        #expect(seal["continuity"]?.object?["stop_reason"]?.string == "maximum-duration")
        #expect(controller.progress()?.maximumReached == false)
        #expect(seal["continuity"]?.object?["received_frames"]?.integer == 480)
        #expect(seal["audio"]?.object?["frames"]?.integer == 480)
        #expect(try controller.waveformSnapshot()?.writtenFrames == 480)
        #expect(try Data(contentsOf: fixture.root.appendingPathComponent(request.originalPath)).dropFirst(44) == pcm(480))
        if let evidenceRoot = ProcessInfo.processInfo.environment["COLE_VOICE_CAPTURE_TEST_EVIDENCE_ROOT"] {
            let evidence = URL(fileURLWithPath: evidenceRoot).appendingPathComponent("wall-deadline-short-take")
            try FileManager.default.copyItem(at: fixture.root, to: evidence)
        }
    }

    @Test func testBlockedTelemetryDoesNotChangeExactWrittenCounts() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
        let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock)
        try controller.start(request: fixture.request())
        try backend.waitUntilStarted()
        for index in 0..<160 {
            try backend.emit(pcm: pcm(480), sampleTime: Double(index * 480), hostTime: fixture.clock.clock.hostTicks())
            fixture.clock.advance(0.01)
            controller.waitForWriterBarrier()
        }
        #expect(controller.progress()?.telemetryDroppedCount == 32)
        #expect(controller.progress()?.writtenFrames == 76_800)
        let overview = try #require(try controller.waveformSnapshot())
        #expect(overview.writtenFrames == 76_800 && overview.endFrame == 76_800)
        #expect(overview.buckets.allSatisfy { $0.minimum == -2_000_000 && $0.maximum == 2_000_000 })
        #expect(overview.sampleClock?.hostTimeTicks == 1_000_000)
        let first = try #require(controller.nextEnvelope())
        let second = try #require(controller.nextEnvelope())
        #expect(first.start_frame == 0 && second.start_frame == 480)
        #expect(second.host_time - first.host_time == 10_000)
        controller.requestStop()
        #expect(try controller.finish().failure == nil)
    }

    @Test func testOwnerLossStopsDeviceAndPersistsPartial() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
        let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock)
        try controller.start(request: fixture.request())
        try backend.waitUntilStarted()
        try backend.emit(pcm: pcm(480), sampleTime: 0, hostTime: fixture.clock.clock.hostTicks())
        fixture.clock.advance(0.01)
        controller.requestStop(parentLost: true)
        let outcome = try controller.finish()
        #expect(backend.isDisposed)
        #expect(outcome.seal == nil && outcome.failure?.code == "RECORDING.PARENT_LOST")
    }

    @Test func testNoFollowAndFaultedInitializerDoNotLeakDescriptors() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        // Suites run in parallel inside one process, so a process-wide descriptor
        // count observes unrelated opens and closes. Every descriptor this writer
        // opens is the fixture asset root or below it, so count exactly those.
        func descriptors() -> Int {
            let owned = fixture.root.path
            let resolved = UnsafeMutablePointer<CChar>.allocate(capacity: Int(MAXPATHLEN))
            defer { resolved.deallocate() }
            return (0..<1024).reduce(0) { total, candidate in
                guard fcntl(Int32(candidate), F_GETPATH, resolved) >= 0 else { return total }
                let path = String(cString: resolved)
                return path == owned || path.hasPrefix(owned + "/") ? total + 1 : total
            }
        }
        let before = descriptors()
        for index in 0..<10 {
            let request = try fixture.request("init-fault-\(index)")
            #expect(throws: NativeCaptureFailure.self) {
                _ = try NativeCaptureWriter(request: request, hook: { point in
                    if case .initialHeader = point { throw NativeCaptureFailure("RECORDING.WRITE_FAILED", "Synthetic header fault") }
                })
            }
        }
        #expect(descriptors() == before)
        let bad = try fixture.request("bad-mode")
        let directory = fixture.root.appendingPathComponent("tmp/recording-takes/bad-mode")
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: directory.path)
        for _ in 0..<10 {
            #expect(throws: NativeCaptureFailure.self) { _ = try NativeCaptureWriter(request: bad, hook: { _ in }) }
        }
        #expect(descriptors() == before)
        let source = try fixture.request("symlink-target")
        let partial = fixture.root.appendingPathComponent(source.partialPath)
        try FileManager.default.createSymbolicLink(atPath: partial.path, withDestinationPath: "/dev/null")
        #expect(throws: NativeCaptureFailure.self) { _ = try NativeCaptureWriter(request: source, hook: { _ in }) }
        #expect(descriptors() == before)
    }
    @Test func testBlockedArchiveQueueBoundsLeasesWhileNewCapturesStopAndSealExactly() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let barrier = TestBarrier()
        let archives = RecordingWaveformArchiveQueue(writeHook: { barrier.hold() })
        defer { barrier.release(); barrier.release(); _ = archives.waitForBarrier() }
        let backend = SyntheticCaptureBackend(clock: fixture.clock.clock)
        let controller = NativeCaptureController(backendFactory: { backend }, clock: fixture.clock.clock)
        var firstDigest = ""
        for index in 0..<100 {
            let name = "archive-take-\(index)"
            try controller.start(request: fixture.request(name))
            try backend.waitUntilStarted()
            try backend.emit(pcm: pcm(480, start: index), sampleTime: 0, hostTime: fixture.clock.clock.hostTicks())
            fixture.clock.advance(0.01)
            controller.requestStop()
            let outcome = try controller.finish()
            let seal = try #require(outcome.seal?.object)
            let digest = try #require(seal["original_sha256"]?.string)
            if index == 0 { firstDigest = digest }
            #expect(outcome.failure == nil && seal["audio"]?.object?["frames"]?.integer == 480)
            #expect(try Data(contentsOf: fixture.root.appendingPathComponent("tmp/recording-takes/\(name)/original.wav")).dropFirst(44) == pcm(480, start: index))
            archives.enqueue(takeID: name, sourceSHA256: digest, assetRoot: fixture.root,
                makeArchive: { try controller.waveformArchiveJob(takeID: name, sourceSHA256: digest, finalWrittenFrames: 480) }, completion: { _ in })
            if index == 0 { try barrier.wait() }
            #expect(archives.pendingCount <= 2)
        }
        #expect(archives.peakPendingCount == 2 && archives.overflowCount == 98)
        barrier.release(); barrier.release()
        #expect(archives.waitForBarrier())
        #expect(archives.pendingCount == 0)
        let historical = try RecordingWaveformArchiveStore.load(sourceSHA256: firstDigest, assetRoot: fixture.root, takeID: "old", processGeneration: "reconnected")
        #expect(try historical.snapshot().writtenFrames == 480 && historical.snapshot().sampleClock == nil)
    }

    @Test func testSlowWallClockSealsHostStampedTakeWithCloseAnchoredOrderedTimestamps() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let skew = SkewClock()
        // 40 ppm slow over one second exceeds the 20,833 ns between the last
        // sample and close. A start-anchored last sample would postdate sealing.
        let (outcome, _) = try skewedTake(fixture, skew, name: "slow-wall-take", wallRate: 1 - 0.000_040)
        #expect(outcome.failure == nil && outcome.recoveryPersisted)
        let seal = try #require(outcome.seal?.object)
        #expect(seal["audio"]?.object?["frames"]?.integer == 48_000)
        let dates = try sealTimestamps(seal)
        #expect(dates[1] == skew.base)
        #expect(abs(dates[2].timeIntervalSince(skew.base) - (0.999_960 - 0.000_020_833)) < 0.000_002)
        #expect(abs(dates[3].timeIntervalSince(dates[2]) - 0.000_020_833) < 0.000_002)
        #expect(dates[1].addingTimeInterval(47_999.0 / 48_000) > dates[3])
    }

    @Test func testFastWallClockKeepsOrderWhenLabelledSpanExceedsNominalDuration() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let skew = SkewClock()
        let (outcome, _) = try skewedTake(fixture, skew, name: "fast-wall-take", wallRate: 1 + 0.000_500)
        #expect(outcome.failure == nil && outcome.recoveryPersisted)
        let seal = try #require(outcome.seal?.object)
        let dates = try sealTimestamps(seal)
        // The documented semantic: last - first carries wall slew, not only (N-1)/48000.
        let span = dates[2].timeIntervalSince(dates[1])
        #expect(span > 47_999.0 / 48_000 + 0.000_400)
        #expect(abs(span - (1.000_500 - 0.000_020_833)) < 0.000_002)
        #expect(seal["audio"]?.object?["duration_seconds"]?.number == 1)
    }

    @Test func testFutureHostStampAndLateRequestRemainInconsistentWithRecoverableMargins() throws {
        for kind in ["future-host", "late-request"] {
            let fixture = try CaptureFixture(); defer { fixture.cleanup() }
            let skew = SkewClock()
            let lateRequest = NativeCaptureTime.string(skew.base.addingTimeInterval(1))
            let (outcome, request) = try skewedTake(fixture, skew, name: "\(kind)-take", wallRate: 1, chunks: 1,
                futureHostNanoseconds: kind == "future-host" ? 1_000_000_000 : 0,
                requestedAtText: kind == "late-request" ? lateRequest : nil)
            #expect(outcome.seal == nil && outcome.recoveryPersisted)
            #expect(outcome.failure?.code == "RECORDING.CONTINUITY_FAILED")
            #expect(outcome.recovery.object?["state"]?.string == "partial")
            #expect(FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent(request.partialPath).path))
            #expect(!FileManager.default.fileExists(atPath: fixture.root.appendingPathComponent(request.originalPath).path))
            let failure = outcome.failure
            #expect(failureDetail(failure, "close_at") == NativeCaptureTime.string(skew.base.addingTimeInterval(0.01)))
            #expect(failureDetail(failure, "close_host_time") == "5010000000")
            #expect(failureDetail(failure, "host_seconds_per_tick") == "1e-09")
            if kind == "future-host" {
                // Margin: last - close = 6,009,979,167 - 5,010,000,000 host ticks.
                #expect(failureDetail(failure, "first_host_time") == "6000000000")
                #expect(failureDetail(failure, "last_host_time") == "6009979167")
                let last = try #require(failureDetail(failure, "last_sample_at").flatMap(NativeCaptureTime.parse))
                #expect(abs(last.timeIntervalSince(skew.base) - 1.009_979_167) < 0.000_002)
            } else {
                // Margin: requested - first = one second of wall time.
                #expect(failureDetail(failure, "requested_at") == lateRequest)
                #expect(failureDetail(failure, "first_sample_at") == NativeCaptureTime.string(skew.base))
                #expect(failureDetail(failure, "first_host_time") == "5000000000")
                #expect(failureDetail(failure, "last_host_time") == "5009979167")
            }
        }
    }

    @Test func testBackwardWallStepDuringSealingPersistsOrderedCloseAnchoredSealTime() throws {
        let fixture = try CaptureFixture(); defer { fixture.cleanup() }
        let skew = SkewClock()
        let (outcome, request) = try skewedTake(fixture, skew, name: "wall-step-take", wallRate: 1, chunks: 1,
            writeHook: { point in if case .fileSync = point { skew.advance(hostNanoseconds: 2_000_000, wallSeconds: -5) } })
        #expect(outcome.failure == nil && outcome.recoveryPersisted)
        let saved = try RecordingJSON.parse(Data(contentsOf: fixture.root.appendingPathComponent(request.originalPath)
            .deletingLastPathComponent().appendingPathComponent("native-recovery.json")))
        #expect(saved == outcome.recovery)
        let seal = try #require(saved.object?["seal"]?.object)
        let dates = try sealTimestamps(seal)
        #expect(abs(dates[2].timeIntervalSince(skew.base) - 0.009_979_167) < 0.000_002)
        #expect(abs(dates[3].timeIntervalSince(skew.base) - 0.012) < 0.000_002)
    }

}
