import AudioToolbox
import CaptureBuffer
import Foundation
import RecordingContracts

public struct NativeCaptureProgress: Sendable {
    public let takeID: String
    public let phase: String
    public let receivedFrames: UInt64
    public let writtenFrames: UInt64
    public let stopRequested: Bool
    public let maximumReached: Bool
    public let telemetryDroppedCount: UInt64
    public let error: NativeCaptureFailure?
}

/// Persistent native recorder lifetime: no device exists between independent takes.
/// Stop writes its C atomic immediately and owns a separate executor from disk/QC work.
public final class NativeCaptureController: @unchecked Sendable {
    private let lock = NSLock()
    private var current: NativeCaptureRun?
    private let backendFactory: @Sendable () -> any NativeCaptureBackend
    private let clock: NativeCaptureClock
    private let writeHook: NativeCaptureWriteHook

    public init(backendFactory: @escaping @Sendable () -> any NativeCaptureBackend = { CoreAudioCaptureBackend() },
                clock: NativeCaptureClock = .system,
                writeHook: @escaping NativeCaptureWriteHook = { _ in }) {
        self.backendFactory = backendFactory
        self.clock = clock
        self.writeHook = writeHook
    }

    public func start(request: NativeCaptureRequest) throws {
        lock.lock()
        defer { lock.unlock() }
        if let current, !current.isFinishedAndDisposed {
            throw NativeCaptureFailure("RECORDING.INVALID_TRANSITION", "A previous capture is still active or requires recovery")
        }
        let run = try NativeCaptureRun(request: request, backend: backendFactory(), clock: clock, writeHook: writeHook)
        current = run
        run.begin()
    }

    public func requestStop(parentLost: Bool = false) {
        lock.lock(); let run = current; lock.unlock()
        run?.requestStop(parentLost: parentLost)
    }

    public func progress() -> NativeCaptureProgress? {
        lock.lock(); let run = current; lock.unlock()
        return run?.progress()
    }

    public func nextEnvelope() -> CVLCaptureEnvelope? {
        lock.lock(); let run = current; lock.unlock()
        return run?.nextEnvelope()
    }

    /// Bounded immutable visual copy. No renderer receives the mutable capture cache.
    public func waveformSnapshot(range: Range<Int64>? = nil, maximumBuckets: Int = 4096) throws -> RecordingWaveformSnapshot? {
        lock.lock(); let run = current; lock.unlock()
        return try run?.waveformSnapshot(range: range, maximumBuckets: maximumBuckets)
    }

    /// Constant-time immutable-source lease for an independent bounded archive executor.
    public func waveformArchiveJob(takeID: String, sourceSHA256: String, finalWrittenFrames: Int64) throws -> @Sendable () throws -> Data {
        lock.lock(); let run = current; lock.unlock()
        guard let run else { throw NativeCaptureFailure("RECORDING.INVALID_TRANSITION", "No sealed take has an archive lease") }
        return { try run.waveformArchive(takeID: takeID, sourceSHA256: sourceSHA256, finalWrittenFrames: finalWrittenFrames) }
    }

    /// Copy derived extrema after successful sealing; call from a background cache executor.
    public func waveformArchive(takeID: String, sourceSHA256: String, finalWrittenFrames: Int64) throws -> Data {
        lock.lock(); let run = current; lock.unlock()
        guard let run else { throw NativeCaptureFailure("RECORDING.INVALID_TRANSITION", "No sealed take has a waveform archive") }
        return try run.waveformArchive(takeID: takeID, sourceSHA256: sourceSHA256, finalWrittenFrames: finalWrittenFrames)
    }

    /// Complete byte-bounded wire snapshot produced independently from reliable Stop/state traffic.
    public func framedWaveformSnapshot(range: Range<Int64>? = nil, meta: RecordingJSON) throws -> Data? {
        lock.lock(); let run = current; lock.unlock()
        return try run?.framedWaveformSnapshot(range: range, meta: meta)
    }

    /// Condition-based completion; call from a worker so the IPC reader can deliver Stop.
    public func finish(timeout: TimeInterval = 15) throws -> NativeCaptureOutcome {
        lock.lock(); let run = current; lock.unlock()
        guard let run else { throw NativeCaptureFailure("RECORDING.INVALID_TRANSITION", "No native take has started") }
        return try run.finish(timeout: timeout)
    }

    /// Deterministic synthetic/test barriers; never waits on UI or a real-time callback.
    public func waitForControlBarrier() {
        lock.lock(); let run = current; lock.unlock()
        run?.controlBarrier()
    }

    public func waitForWriterBarrier() {
        lock.lock(); let run = current; lock.unlock()
        run?.writerBarrier()
    }

    /// Trigger the same heartbeat used by native lifecycle timers, useful with injected clocks.
    public func checkNow() {
        lock.lock(); let run = current; lock.unlock()
        run?.checkNow()
    }

    deinit {
        current?.requestStop(parentLost: true)
    }
}

private final class NativeCaptureRun: @unchecked Sendable {
    private let request: NativeCaptureRequest
    private let backend: any NativeCaptureBackend
    private let clock: NativeCaptureClock
    private let context: OpaquePointer
    private let writer: NativeCaptureWriter
    private let condition = NSCondition()
    private var outcome: NativeCaptureOutcome?
    private var failure: NativeCaptureFailure?
    private var phase = "starting"
    private var stopReason = "requested"
    private let control = DispatchQueue(label: "cole.voice.native.capture.control", qos: .userInteractive)
    private let writing = DispatchQueue(label: "cole.voice.native.capture.writer", qos: .userInitiated)
    private var controlTimer: DispatchSourceTimer?
    private var writerTimer: DispatchSourceTimer?
    private var stopDelivered = false // control executor only
    private var writerFinalized = false // writer executor only
    private var writerFailed = false // writer executor only
    private let startedMonotonic: Double
    private let anchorDate: Date
    private let anchorHost: UInt64

    init(request: NativeCaptureRequest, backend: any NativeCaptureBackend,
         clock: NativeCaptureClock, writeHook: @escaping NativeCaptureWriteHook) throws {
        guard clock.hostSecondsPerTick.isFinite, clock.hostSecondsPerTick > 0 else {
            throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Host clock scale must be finite and positive")
        }
        self.request = request
        self.backend = backend
        self.clock = clock
        self.startedMonotonic = clock.monotonicSeconds()
        self.anchorDate = clock.now()
        self.anchorHost = clock.hostTicks()
        var configuration = cvl_capture_default_configuration(request.bufferGeneration)
        configuration.maximum_frames = request.maximumFrames
        var error = CVL_CAPTURE_OK
        guard let allocated = cvl_capture_create(configuration, &error) else {
            throw NativeCaptureFailure("RECORDING.WRITE_FAILED", "Cannot allocate native capture buffers",
                                       details: [String(cString: cvl_capture_error_code(error))])
        }
        self.context = allocated
        do { self.writer = try NativeCaptureWriter(request: request, hook: writeHook, hostSecondsPerTick: clock.hostSecondsPerTick) }
        catch {
            cvl_capture_request_stop(allocated)
            _ = cvl_capture_mark_queue_disposed(allocated)
            _ = cvl_capture_free(allocated)
            throw error
        }
    }

    var isFinishedAndDisposed: Bool {
        condition.lock(); defer { condition.unlock() }
        return outcome != nil && cvl_capture_snapshot(context).queue_disposed
    }

    func begin() {
        writing.async { [self] in
            let timer = DispatchSource.makeTimerSource(queue: writing)
            timer.schedule(deadline: .now(), repeating: .milliseconds(2), leeway: .milliseconds(1))
            timer.setEventHandler { [weak self] in self?.writeAvailable() }
            writerTimer = timer
            timer.resume()
        }
        control.async { [self] in
            do {
                if cvl_capture_snapshot(context).stop_requested { stopOnControl(); return }
                try backend.open(deviceUID: request.deviceUID, context: context)
                if cvl_capture_snapshot(context).stop_requested { stopOnControl(); return }
                try backend.start()
                let timer = DispatchSource.makeTimerSource(queue: control)
                timer.schedule(deadline: .now(), repeating: .milliseconds(20), leeway: .milliseconds(2))
                timer.setEventHandler { [weak self] in self?.heartbeat() }
                controlTimer = timer
                timer.resume()
                heartbeat()
            } catch { fail(Self.wrap(error)); stopOnControl() }
        }
    }

    func requestStop(parentLost: Bool) {
        condition.lock(); let completed = outcome != nil; condition.unlock()
        if completed { return }
        if parentLost {
            fail(NativeCaptureFailure("RECORDING.PARENT_LOST", "Recorder owner disconnected before capture completed"))
            cvl_capture_fail(context, CVL_CAPTURE_PARENT_LOST)
        }
        cvl_capture_request_stop(context)
        condition.lock()
        if outcome == nil { phase = "stopping" }
        condition.unlock()
        // The queued closure retains the run through asynchronous teardown even
        // when the persistent controller is deallocated during capture.
        control.async { [self] in stopOnControl() }
    }

    private func heartbeat() {
        guard !stopDelivered else { return }
        let snapshot = cvl_capture_snapshot(context)
        if let error = NativeCaptureFailure.capture(snapshot) { fail(error); stopOnControl(); return }
        let elapsed = clock.monotonicSeconds() - startedMonotonic
        if !elapsed.isFinite || elapsed < 0 {
            fail(NativeCaptureFailure("RECORDING.CONTINUITY_FAILED", "Native monotonic capture clock changed"))
            stopOnControl(); return
        }
        if snapshot.maximum_reached || elapsed >= Double(request.maximumFrames) / 48_000 {
            condition.lock(); stopReason = "maximum-duration"; condition.unlock()
            cvl_capture_request_stop(context)
        }
        if cvl_capture_snapshot(context).stop_requested { stopOnControl(); return }
        if snapshot.received_frames == 0 && elapsed >= 10 {
            fail(NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Microphone produced no samples before startup timeout"))
            stopOnControl(); return
        }
        do { try backend.checkHealth() }
        catch { fail(Self.wrap(error)); stopOnControl(); return }
        if snapshot.received_frames > 0 {
            condition.lock(); if outcome == nil && phase == "starting" { phase = "recording" }; condition.unlock()
        }
    }

    private func stopOnControl() {
        guard !stopDelivered else { return }
        stopDelivered = true
        cvl_capture_request_stop(context)
        condition.lock(); if outcome == nil { phase = "stopping" }; condition.unlock()
        controlTimer?.cancel(); controlTimer = nil
        do { try backend.stopAndDispose() }
        catch { fail(Self.wrap(error)) }
        guard backend.isDisposed else {
            fail(NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native queue disposal failed; recorder remains locked for recovery"))
            // Context cannot be freed while the backend might still invoke it.
            // The controller remains unavailable and completion reports a timeout.
            return
        }
        guard cvl_capture_mark_queue_disposed(context) else {
            fail(NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native callbacks remained active after disposal"))
            return
        }
        writing.async { [self] in writeAvailable() }
    }

    private func fail(_ error: NativeCaptureFailure) {
        condition.lock()
        if failure == nil { failure = error }
        condition.unlock()
    }

    private static func wrap(_ error: Error) -> NativeCaptureFailure {
        if let error = error as? NativeCaptureFailure { return error }
        return NativeCaptureFailure("RECORDING.WRITE_FAILED", "Native capture operation failed",
                                    details: [String(describing: type(of: error))])
    }

    private func writeAvailable() {
        guard !writerFinalized else { return }
        if !writerFailed {
            do { try writer.drain(context) }
            catch {
                writerFailed = true
                fail(Self.wrap(error))
                cvl_capture_fail(context, CVL_CAPTURE_WRITER_FAILED)
                control.async { [self] in stopOnControl() }
            }
        }
        let snapshot = cvl_capture_snapshot(context)
        guard snapshot.queue_disposed else { return }
        if let error = NativeCaptureFailure.capture(snapshot) { fail(error) }
        condition.lock(); let capturedFailure = failure; condition.unlock()
        if snapshot.queued_frames > 0 && !writerFailed { return }
        writerFinalized = true
        writerTimer?.cancel(); writerTimer = nil
        finalize(snapshot, error: capturedFailure)
    }

    private func finalize(_ snapshot: CVLCaptureSnapshot, error: NativeCaptureFailure?) {
        var finalFailure = error
        var seal: RecordingJSON?
        if finalFailure == nil {
            do {
                guard snapshot.received_frames > 0 else {
                    throw NativeCaptureFailure("RECORDING.CANCELLED", "Capture stopped before any complete PCM frames arrived")
                }
                guard snapshot.first_host_time_valid else {
                    throw NativeCaptureFailure("RECORDING.CONTINUITY_FAILED", "First sample has no valid Core Audio host timestamp")
                }
                let hostDelta = snapshot.first_host_time >= anchorHost
                    ? Double(snapshot.first_host_time - anchorHost)
                    : -Double(anchorHost - snapshot.first_host_time)
                let firstAt = anchorDate.addingTimeInterval(hostDelta * clock.hostSecondsPerTick)
                // The last sample is labelled from a wall/host pair read at close, so
                // wall-clock slew accumulated since the start anchor cannot date it
                // after close. Future-dated samples are rejected on the host clock.
                let closeDate = clock.now()
                let closeHost = clock.hostTicks()
                var projectedLastHost: UInt64?
                if snapshot.written_frames > 0,
                   let span = UInt64(exactly: (Double(snapshot.written_frames - 1) / 48_000 / clock.hostSecondsPerTick).rounded()) {
                    let sum = snapshot.first_host_time.addingReportingOverflow(span)
                    if !sum.overflow { projectedLastHost = sum.partialValue }
                }
                let projectedLastAt = projectedLastHost.map { host in
                    closeDate.addingTimeInterval(host <= closeHost ? -Double(closeHost - host) * clock.hostSecondsPerTick
                                                                   : Double(host - closeHost) * clock.hostSecondsPerTick)
                }
                guard let lastHost = projectedLastHost, let lastAt = projectedLastAt, request.requestedAt <= firstAt,
                      snapshot.first_host_time <= lastHost, lastHost <= closeHost, firstAt <= lastAt else {
                    throw NativeCaptureFailure("RECORDING.CONTINUITY_FAILED", "Requested, captured and sealing timestamps are inconsistent",
                        details: ["requested_at=\(request.requestedAtText)", "first_sample_at=\(NativeCaptureTime.string(firstAt))",
                                  "last_sample_at=\(projectedLastAt.map { NativeCaptureTime.string($0) } ?? "unavailable")",
                                  "close_at=\(NativeCaptureTime.string(closeDate))",
                                  "first_host_time=\(snapshot.first_host_time)",
                                  "last_host_time=\(projectedLastHost.map { String($0) } ?? "unavailable")",
                                  "close_host_time=\(closeHost)", "host_seconds_per_tick=\(clock.hostSecondsPerTick)"])
                }
                let original = try writer.seal(snapshot: snapshot)
                // Close-anchored and ordered after lastAt by construction; nothing may
                // throw once the WAV is published, even if the wall clock stepped.
                let sealedHost = clock.hostTicks()
                let sealedAt = closeDate.addingTimeInterval(
                    Double(sealedHost >= closeHost ? sealedHost - closeHost : 0) * clock.hostSecondsPerTick)
                condition.lock(); let reason = stopReason; condition.unlock()
                seal = .object([
                    "schema_version": .string("cole-voice-lab.capture-seal/v1"),
                    "take_id": .string(request.takeID), "reservation_sha256": .string(request.reservationSHA256),
                    "process_generation": .string(request.processGeneration), "recorder_build": request.recorderBuild,
                    "original_path": .string(request.originalPath), "original_sha256": .string(original.sha256),
                    "original_size_bytes": .integer(Int64(original.size)),
                    "audio": .object(["sample_rate_hz": .integer(48_000), "channels": .integer(1),
                                      "sample_width_bytes": .integer(3), "encoding": .string("pcm_s24le"),
                                      "frames": .integer(Int64(snapshot.written_frames)),
                                      "duration_seconds": .number(Double(snapshot.written_frames) / 48_000)]),
                    "requested_at": .string(request.requestedAtText),
                    "first_sample_at": .string(NativeCaptureTime.string(firstAt)),
                    "last_sample_at": .string(NativeCaptureTime.string(lastAt)),
                    "sealed_at": .string(NativeCaptureTime.string(sealedAt)),
                    "continuity": .object([
                        "schema_version": .string("cole-voice-lab.capture-continuity/v1"),
                        "backend": .string("coreaudio-audioqueue"), "device_uid": .string(request.deviceUID),
                        "output_name": .string("original.wav"), "wav_sha256": .string(original.sha256),
                        "wav_size_bytes": .integer(Int64(original.size)), "frames": .integer(Int64(snapshot.written_frames)),
                        "received_frames": .integer(Int64(snapshot.received_frames)),
                        "callback_count": .integer(Int64(snapshot.callback_count)),
                        "first_sample_time": .number(snapshot.first_sample_time),
                        "last_sample_end": .number(snapshot.next_sample_time), "success": .boolean(true),
                        "continuity_checked": .boolean(true), "stop_reason": .string(reason),
                    ]),
                ])
            } catch { finalFailure = Self.wrap(error) }
        }
        func recovery(_ seal: RecordingJSON?, _ failure: NativeCaptureFailure?) -> RecordingJSON {
            .object([
                "schema_version": .string("cole-voice-lab.native-recovery/v1"),
                "take_id": .string(request.takeID), "reservation_sha256": .string(request.reservationSHA256),
                "process_generation": .string(request.processGeneration),
                "recorded_at": .string(NativeCaptureTime.string(clock.now())),
                "partial_path": .string(request.partialPath),
                "received_frames": .integer(Int64(snapshot.received_frames)),
                "written_frames": .integer(Int64(snapshot.written_frames)),
                "state": .string(failure == nil ? "sealed" : "partial"),
                "seal": seal ?? .null, "error": failure?.json ?? .null,
            ])
        }
        var evidence = recovery(seal, finalFailure)
        var persisted = false
        do { try writer.persistRecovery(evidence); persisted = true }
        catch {
            finalFailure = Self.wrap(error)
            seal = nil
            evidence = recovery(nil, finalFailure)
        }
        let result = NativeCaptureOutcome(recovery: evidence, seal: seal,
                                          failure: finalFailure, recoveryPersisted: persisted)
        condition.lock()
        phase = finalFailure == nil ? "draft" : "error"
        failure = finalFailure
        outcome = result
        condition.broadcast()
        condition.unlock()
    }

    func progress() -> NativeCaptureProgress {
        let snapshot = cvl_capture_snapshot(context)
        condition.lock(); let currentPhase = phase; let currentFailure = failure; condition.unlock()
        return NativeCaptureProgress(takeID: request.takeID, phase: currentPhase,
            receivedFrames: snapshot.received_frames, writtenFrames: snapshot.written_frames,
            stopRequested: snapshot.stop_requested, maximumReached: snapshot.maximum_reached,
            telemetryDroppedCount: snapshot.telemetry_dropped_count,
            error: currentFailure ?? NativeCaptureFailure.capture(snapshot))
    }

    func nextEnvelope() -> CVLCaptureEnvelope? {
        var envelope = CVLCaptureEnvelope()
        return cvl_capture_read_envelope(context, &envelope) ? envelope : nil
    }

    func waveformArchive(takeID: String, sourceSHA256: String, finalWrittenFrames: Int64) throws -> Data {
        condition.lock(); let seal = outcome?.seal; condition.unlock()
        guard takeID == request.takeID, seal?.object?["original_sha256"] == .string(sourceSHA256),
              seal?.object?["audio"]?.object?["frames"] == .integer(finalWrittenFrames) else {
            throw NativeCaptureFailure("RECORDING.SOURCE_CHANGED", "Waveform archive does not identify the successfully sealed current take")
        }
        return try writer.waveform.archive(sourceSHA256: sourceSHA256, finalWrittenFrames: finalWrittenFrames)
    }

    func waveformSnapshot(range: Range<Int64>?, maximumBuckets: Int) throws -> RecordingWaveformSnapshot {
        try writer.waveform.snapshot(range: range, maximumBuckets: maximumBuckets)
    }

    func framedWaveformSnapshot(range: Range<Int64>?, meta: RecordingJSON) throws -> Data {
        try writer.waveform.framedSnapshot(range: range, meta: meta).frame
    }

    func finish(timeout: TimeInterval) throws -> NativeCaptureOutcome {
        let until = Date(timeIntervalSinceNow: timeout)
        condition.lock(); defer { condition.unlock() }
        while outcome == nil {
            guard condition.wait(until: until) else {
                throw NativeCaptureFailure("RECORDING.DEVICE_UNAVAILABLE", "Native capture did not finish within its bounded shutdown deadline")
            }
        }
        return outcome!
    }

    func controlBarrier() { control.sync {} }
    func writerBarrier() { writing.sync { writeAvailable() } }
    func checkNow() { control.sync { heartbeat() } }

    deinit {
        // Every successful lifecycle disposes before the final strong reference
        // disappears. Refusal deliberately leaks C storage instead of creating
        // a use-after-free after an unproved native disposal.
        _ = cvl_capture_free(context)
    }
}
