import CaptureBuffer
import Foundation
@testable import RecordingCore
import Testing

private final class SyntheticTimerEventBarrier: @unchecked Sendable {
    private let lock = NSLock()
    private let entered = DispatchSemaphore(value: 0)
    private let proceed = DispatchSemaphore(value: 0)
    private var counts: [UInt] = []
    func observe(_ expirations: UInt) {
        lock.lock(); counts.append(expirations); let count = counts.count; lock.unlock()
        if count <= 2 { entered.signal(); proceed.wait() }
    }
    func wait() throws {
        guard entered.wait(timeout: .now() + 5) == .success else {
            throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Synthetic timer event barrier timed out")
        }
    }
    func release() { proceed.signal() }
    var expirations: [UInt] { lock.lock(); defer { lock.unlock() }; return counts }
}

private final class SyntheticTimerCaptureBuffer {
    let context: OpaquePointer
    init(maximum: UInt64 = 48_000, capacity: UInt32 = UInt32(CVL_CAPTURE_DEFAULT_RAW_CAPACITY_FRAMES)) throws {
        var configuration = cvl_capture_default_configuration(1)
        configuration.maximum_frames = maximum
        configuration.raw_capacity_frames = capacity
        var error = CVL_CAPTURE_OK
        context = try #require(cvl_capture_create(configuration, &error))
    }
    func pcm() -> Data {
        let frames = cvl_capture_snapshot(context).queued_frames
        var bytes = Data(count: Int(frames) * 3)
        let count = bytes.withUnsafeMutableBytes { raw in cvl_capture_read_pcm(context, raw.baseAddress, frames) }
        #expect(count == frames)
        return bytes
    }
    deinit {
        cvl_capture_request_stop(context)
        _ = cvl_capture_mark_queue_disposed(context)
        _ = cvl_capture_free(context)
    }
}

private func syntheticTimerPattern(frames: Int) -> Data {
    Data((0..<(frames * 3)).map { UInt8($0 % 109) })
}

private func syntheticTimerExpected(_ pattern: Data, frames: Int) -> Data {
    Data((0..<(frames * 3)).map { pattern[$0 % pattern.count] })
}

@Suite(.serialized) struct SyntheticCaptureBackendTests {
    @Test func coalescedTimerExpirationsEmitEveryElapsedChunkAndStopDoesNotReplay() throws {
        let pattern = syntheticTimerPattern(frames: 37)
        let buffer = try SyntheticTimerCaptureBuffer()
        let gate = SyntheticTimerEventBarrier()
        let backend = SyntheticCaptureBackend(pcm: pattern, chunkFrames: 480, totalFrames: 48_000)
        backend.setTimerEventHook { gate.observe($0) }
        defer { gate.release(); gate.release(); try? backend.stopAndDispose() }
        try backend.open(deviceUID: "synthetic", context: buffer.context)
        try backend.start()
        try gate.wait()
        // The first completed event stays blocked while real timer expirations accumulate.
        Thread.sleep(forTimeInterval: 0.055)
        gate.release()
        try gate.wait()
        let expirations = gate.expirations
        #expect(expirations.count == 2 && expirations[1] > 1)
        let expected = min(UInt64(48_000), UInt64(expirations.reduce(0, +)) * 480)
        let observed = cvl_capture_snapshot(buffer.context)
        #expect(observed.received_frames == expected)
        #expect(observed.next_sample_time == Double(expected))
        #expect(observed.error == CVL_CAPTURE_OK)
        let clock = NativeCaptureClock.system
        let elapsed = Double(clock.hostTicks() - observed.first_host_time) * clock.hostSecondsPerTick
        #expect(Double(expected) / 48_000 <= elapsed)
        #expect(buffer.pcm() == syntheticTimerExpected(pattern, frames: Int(expected)))
        try backend.stopAndDispose()
        gate.release()
        backend.emitTimerExpirations(100, generation: backend.captureGeneration)
        #expect(cvl_capture_snapshot(buffer.context).received_frames == expected)
        #expect(backend.disposedCount == 1)
    }

    @Test func coalescedExpirationsCapAtTheDeclaredFiniteStreamIncludingPartialChunk() throws {
        let pattern = syntheticTimerPattern(frames: 37)
        let buffer = try SyntheticTimerCaptureBuffer()
        let backend = SyntheticCaptureBackend(pcm: pattern, chunkFrames: 480, totalFrames: 1_733)
        defer { try? backend.stopAndDispose() }
        try backend.open(deviceUID: "synthetic", context: buffer.context)
        backend.emitTimerExpirations(UInt.max, generation: backend.captureGeneration)
        let observed = cvl_capture_snapshot(buffer.context)
        #expect(observed.received_frames == 1_733 && observed.next_sample_time == 1_733)
        #expect(observed.error == CVL_CAPTURE_OK)
        #expect(buffer.pcm() == syntheticTimerExpected(pattern, frames: 1_733))
        backend.emitTimerExpirations(20, generation: backend.captureGeneration)
        #expect(cvl_capture_snapshot(buffer.context).received_frames == 1_733)
    }

    @Test func anExpiredEventFromThePreviousCaptureCannotEmitIntoTheReopenedBackend() throws {
        let oldBuffer = try SyntheticTimerCaptureBuffer()
        let newBuffer = try SyntheticTimerCaptureBuffer()
        let backend = SyntheticCaptureBackend(pcm: syntheticTimerPattern(frames: 37), repeatPCM: true)
        defer { try? backend.stopAndDispose() }
        try backend.open(deviceUID: "synthetic", context: oldBuffer.context)
        let oldGeneration = backend.captureGeneration
        backend.emitTimerExpirations(1, generation: oldGeneration)
        try backend.stopAndDispose()
        try backend.open(deviceUID: "synthetic", context: newBuffer.context)
        let newGeneration = backend.captureGeneration
        #expect(newGeneration > oldGeneration)
        // Model an old handler paused after reading its count but before applying it.
        backend.emitTimerExpirations(5, generation: oldGeneration)
        #expect(cvl_capture_snapshot(newBuffer.context).received_frames == 0)
        backend.emitTimerExpirations(1, generation: newGeneration)
        #expect(cvl_capture_snapshot(newBuffer.context).received_frames == 480)
        #expect(cvl_capture_snapshot(oldBuffer.context).received_frames == 480)
    }

    @Test func excessiveCoalescedBacklogFailsBeforeUnboundedEmission() throws {
        let buffer = try SyntheticTimerCaptureBuffer(maximum: 28_800_000)
        let backend = SyntheticCaptureBackend(pcm: syntheticTimerPattern(frames: 37), repeatPCM: true)
        defer { try? backend.stopAndDispose() }
        try backend.open(deviceUID: "synthetic", context: buffer.context)
        backend.emitTimerExpirations(UInt.max, generation: backend.captureGeneration)
        let observed = cvl_capture_snapshot(buffer.context)
        #expect(observed.error == CVL_CAPTURE_RAW_OVERFLOW && observed.stop_requested)
        #expect(observed.received_frames == 0)
        backend.emitTimerExpirations(1, generation: backend.captureGeneration)
        #expect(cvl_capture_snapshot(buffer.context).received_frames == 0)
    }

    @Test func coalescedChunksStillFailClosedWhenTheActualRingIsFull() throws {
        let buffer = try SyntheticTimerCaptureBuffer(capacity: 4_096)
        let backend = SyntheticCaptureBackend(pcm: syntheticTimerPattern(frames: 37), repeatPCM: true)
        defer { try? backend.stopAndDispose() }
        try backend.open(deviceUID: "synthetic", context: buffer.context)
        backend.emitTimerExpirations(10, generation: backend.captureGeneration)
        let observed = cvl_capture_snapshot(buffer.context)
        #expect(observed.error == CVL_CAPTURE_RAW_OVERFLOW && observed.stop_requested)
        #expect(observed.received_frames == 3_840)
        backend.emitTimerExpirations(10, generation: backend.captureGeneration)
        #expect(cvl_capture_snapshot(buffer.context).received_frames == 3_840)
    }
}
