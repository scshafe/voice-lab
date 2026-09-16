import AudioToolbox
import CaptureBuffer
import Foundation

/// Explicitly injected synthetic device. It exercises the canonical C receive path
/// shared by the real AudioQueue callback and never creates a device or audio output.
public final class SyntheticCaptureBackend: NativeCaptureBackend, @unchecked Sendable {
    private let condition = NSCondition()
    private var context: OpaquePointer?
    private var started = false
    private var disposed = true
    private var timer: DispatchSourceTimer?
    private let executor = DispatchQueue(label: "cole.voice.synthetic.capture")
    private let pcm: Data?
    private let chunkFrames: Int
    private let repeatPCM: Bool
    private let stopAtEnd: Bool
    private let totalFrames: UInt64?
    private let clock: NativeCaptureClock
    private var byteOffset = 0
    private var emittedFrames: UInt64 = 0
    private var firstHost: UInt64 = 0
    private var healthFailure: NativeCaptureFailure?
    private var stopPCM: Data?
    private var startHook: (@Sendable () throws -> Void)?
    private var timerEventHook: (@Sendable (UInt) -> Void)?
    private var openCount = 0
    private var closeCount = 0
    private var generation: UInt64 = 0

    public init(pcm: Data? = nil, chunkFrames: Int = 480, repeatPCM: Bool = false,
                stopAtEnd: Bool = false, totalFrames: UInt64? = nil, clock: NativeCaptureClock = .system) {
        self.pcm = pcm
        self.chunkFrames = chunkFrames
        self.repeatPCM = repeatPCM || totalFrames != nil
        self.totalFrames = totalFrames
        self.stopAtEnd = stopAtEnd
        self.clock = clock
    }

    public var isDisposed: Bool { condition.lock(); defer { condition.unlock() }; return disposed }
    public var openedCount: Int { condition.lock(); defer { condition.unlock() }; return openCount }
    public var disposedCount: Int { condition.lock(); defer { condition.unlock() }; return closeCount }
    var captureGeneration: UInt64 { condition.lock(); defer { condition.unlock() }; return generation }

    public func setStartHook(_ hook: @escaping @Sendable () throws -> Void) {
        condition.lock(); startHook = hook; condition.unlock()
    }

    /// Observe completed dispatch events outside the capture lock in synthetic tests.
    func setTimerEventHook(_ hook: @escaping @Sendable (UInt) -> Void) {
        condition.lock(); timerEventHook = hook; condition.unlock()
    }

    public func injectHealthFailure(_ failure: NativeCaptureFailure) {
        condition.lock(); healthFailure = failure; condition.unlock()
    }

    /// Model a final partial buffer invoked synchronously during native Stop.
    public func setStopDrainPCM(_ pcm: Data) {
        condition.lock(); stopPCM = pcm; condition.unlock()
    }

    public func open(deviceUID: String, context: OpaquePointer) throws {
        condition.lock(); defer { condition.unlock() }
        guard disposed, generation < UInt64.max, chunkFrames > 0, chunkFrames <= 4096,
              totalFrames == nil || (pcm != nil && (1...28_800_000).contains(totalFrames!)),
              pcm == nil || (pcm!.count > 0 && pcm!.count % 3 == 0) else {
            throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Invalid synthetic capture source or repeated open")
        }
        self.context = context
        generation += 1
        disposed = false
        started = false
        byteOffset = 0
        emittedFrames = 0
        openCount += 1
    }

    public func start() throws {
        condition.lock(); let hook = startHook; condition.unlock()
        try hook?()
        condition.lock()
        guard let context else {
            condition.unlock()
            throw NativeCaptureFailure("RECORDING.INVALID_TRANSITION", "Synthetic backend has no prepared context")
        }
        if cvl_capture_snapshot(context).stop_requested {
            condition.broadcast(); condition.unlock(); return
        }
        firstHost = clock.hostTicks()
        let scheduledGeneration = generation
        started = true
        condition.broadcast()
        condition.unlock()
        guard pcm != nil else { return }
        let source = DispatchSource.makeTimerSource(queue: executor)
        let interval = Double(chunkFrames) / 48_000
        source.schedule(deadline: .now() + interval, repeating: interval, leeway: .microseconds(100))
        source.setEventHandler { [weak self] in self?.emitTimerEvent(generation: scheduledGeneration) }
        condition.lock(); timer = source; condition.unlock()
        source.resume()
    }

    public func waitUntilStarted(timeout: TimeInterval = 5) throws {
        let deadline = Date(timeIntervalSinceNow: timeout)
        condition.lock(); defer { condition.unlock() }
        while !started {
            guard condition.wait(until: deadline) else {
                throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Synthetic startup barrier timed out")
            }
        }
    }

    public func checkHealth() throws {
        condition.lock(); let failure = healthFailure; condition.unlock()
        if let failure { throw failure }
    }

    private func emitLocked(_ pcm: Data, sampleTime: Double, hostTime: UInt64,
                            timestampFlags: UInt32) throws {
        guard let context, !disposed else {
            throw NativeCaptureFailure("RECORDING.INVALID_TRANSITION", "Synthetic capture is not open")
        }
        guard pcm.count <= 4096 * 3 else {
            throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Synthetic callback exceeds its native buffer bound")
        }
        let received = pcm.withUnsafeBytes { bytes in
            cvl_capture_receive_pcm(context, bytes.baseAddress, UInt32(bytes.count), UInt32(bytes.count),
                                    sampleTime, timestampFlags, hostTime)
        }
        if !received, let failure = NativeCaptureFailure.capture(cvl_capture_snapshot(context)) { throw failure }
        if received { emittedFrames += UInt64(pcm.count / 3) }
    }

    public func emit(pcm: Data, sampleTime: Double, hostTime: UInt64,
                     timestampFlags: UInt32 = AudioTimeStampFlags.sampleTimeValid.rawValue | AudioTimeStampFlags.hostTimeValid.rawValue) throws {
        condition.lock(); defer { condition.unlock() }
        try emitLocked(pcm, sampleTime: sampleTime, hostTime: hostTime, timestampFlags: timestampFlags)
    }

    private func emitTimerEvent(generation: UInt64) {
        condition.lock()
        guard generation == self.generation else { condition.unlock(); return }
        let expirations = timer?.data ?? 0
        let hook = timerEventHook
        condition.unlock()
        emitTimerExpirations(expirations, generation: generation)
        hook?(expirations)
    }

    /// Account for elapsed timer intervals without unbounded catch-up work. The
    /// canonical C receiver separately enforces actual ring occupancy on each chunk.
    func emitTimerExpirations(_ expirations: UInt, generation: UInt64) {
        condition.lock(); defer { condition.unlock() }
        guard generation == self.generation,
              let context, let pcm, !disposed, expirations > 0,
              !cvl_capture_snapshot(context).stop_requested else { return }
        let remaining = totalFrames.map { $0 > emittedFrames ? $0 - emittedFrames : 0 }
            ?? (repeatPCM ? UInt64.max : UInt64((pcm.count - byteOffset) / 3))
        if remaining == 0 { emitNextChunkLocked(); return }
        let budget = UInt64(CVL_CAPTURE_DEFAULT_RAW_CAPACITY_FRAMES)
        // Bound before multiplication, including absurd injected expiration counts.
        // Finite sources only owe the frames still present in their declared stream.
        let needed = (min(remaining, budget + 1) + UInt64(chunkFrames) - 1) / UInt64(chunkFrames)
        let chunks = min(UInt64(expirations), needed)
        guard min(remaining, chunks * UInt64(chunkFrames)) <= budget else {
            timer?.cancel(); timer = nil
            cvl_capture_fail(context, CVL_CAPTURE_RAW_OVERFLOW)
            return
        }
        for _ in 0..<chunks {
            guard !cvl_capture_snapshot(context).stop_requested else { return }
            emitNextChunkLocked()
        }
    }

    private func emitNextChunkLocked() {
        guard let context, let pcm, !disposed, !cvl_capture_snapshot(context).stop_requested else { return }
        if let totalFrames, emittedFrames >= totalFrames {
            timer?.cancel(); timer = nil
            if stopAtEnd { cvl_capture_request_stop(context) }
            return
        }
        let desired = min(UInt64(chunkFrames), totalFrames.map { $0 - emittedFrames } ?? UInt64(chunkFrames))
        var payload = Data()
        payload.reserveCapacity(Int(desired) * 3)
        while payload.count < Int(desired) * 3 {
            if byteOffset == pcm.count {
                if repeatPCM { byteOffset = 0 }
                else { break }
            }
            let count = min(Int(desired) * 3 - payload.count, pcm.count - byteOffset)
            payload.append(pcm.subdata(in: byteOffset..<(byteOffset + count)))
            byteOffset += count
        }
        guard !payload.isEmpty else {
            timer?.cancel(); timer = nil
            if stopAtEnd { cvl_capture_request_stop(context) }
            return
        }
        let delta = Double(emittedFrames) / 48_000 / clock.hostSecondsPerTick
        let host = firstHost + UInt64(delta.rounded())
        do {
            try emitLocked(payload, sampleTime: Double(emittedFrames), hostTime: host,
                           timestampFlags: AudioTimeStampFlags.sampleTimeValid.rawValue | AudioTimeStampFlags.hostTimeValid.rawValue)
            if let totalFrames, emittedFrames >= totalFrames {
                timer?.cancel(); timer = nil
                if stopAtEnd { cvl_capture_request_stop(context) }
            }
        } catch {
            timer?.cancel(); timer = nil
            cvl_capture_request_stop(context)
        }
    }

    public func stopAndDispose() throws {
        condition.lock(); defer { condition.unlock() }
        guard !disposed else { return }
        timer?.cancel(); timer = nil
        defer {
            disposed = true
            started = false
            context = nil
            closeCount += 1
            condition.broadcast()
        }
        if let stopPCM {
            self.stopPCM = nil
            let host = firstHost + UInt64((Double(emittedFrames) / 48_000 / clock.hostSecondsPerTick).rounded())
            try emitLocked(stopPCM, sampleTime: Double(emittedFrames), hostTime: host,
                           timestampFlags: AudioTimeStampFlags.sampleTimeValid.rawValue | AudioTimeStampFlags.hostTimeValid.rawValue)
        }
    }
}
