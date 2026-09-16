import Foundation
import RecordingContracts

/// Accessed only on the archive executor after construction. Releases the capture run
/// immediately after serialization, before filesystem IO and completion callbacks.
private final class RecordingWaveformArchiveLease: @unchecked Sendable {
    private var archive: (@Sendable () throws -> Data)?
    init(_ archive: @escaping @Sendable () throws -> Data) { self.archive = archive }
    func release() { archive = nil }
    func encodeAndRelease() throws -> Data {
        guard let archive else { throw RecordingContractError(code: "RECORDING.WAVEFORM_CACHE_UNAVAILABLE", message: "Archive lease was already consumed") }
        defer { self.archive = nil }
        return try archive()
    }
}

public struct RecordingWaveformArchiveCompletion: Sendable {
    public let takeID: String
    public let sourceSHA256: String
    public let result: RecordingWaveformArchiveResult
}

/// At most two sealed-run leases can outlive capture while derived archives are pending.
/// Overflow is an explicit rebuildable cache miss, never capture failure or unbounded retention.
public final class RecordingWaveformArchiveQueue: @unchecked Sendable {
    private let queue = DispatchQueue(label: "cole.recording.waveform-archives", qos: .utility)
    private let lock = NSLock()
    private var pending = 0
    private var peak = 0
    private var overflows = 0
    private let writeHook: @Sendable () throws -> Void
    private let maximumPending: Int

    public init(maximumPending: Int = 2, writeHook: @escaping @Sendable () throws -> Void = {}) {
        precondition((1...2).contains(maximumPending))
        self.maximumPending = maximumPending; self.writeHook = writeHook
    }
    public var pendingCount: Int { lock.lock(); defer { lock.unlock() }; return pending }
    public var peakPendingCount: Int { lock.lock(); defer { lock.unlock() }; return peak }
    public var overflowCount: Int { lock.lock(); defer { lock.unlock() }; return overflows }

    /// `makeArchive` creates a constant-time sealed-run lease only after capacity is reserved.
    /// All bucket serialization and filesystem work occurs on the independent utility executor.
    public func enqueue(takeID: String, sourceSHA256: String, assetRoot: URL,
                        makeArchive: () throws -> @Sendable () throws -> Data,
                        completion: @escaping @Sendable (RecordingWaveformArchiveCompletion) -> Void) {
        lock.lock()
        guard pending < maximumPending else {
            overflows += 1; lock.unlock()
            completion(.init(takeID: takeID, sourceSHA256: sourceSHA256, result: .needsRebuild(failure("Background waveform archive lease bound is full; selected source requires cache rebuild"))))
            return
        }
        pending += 1; peak = max(peak, pending); lock.unlock()
        let archive: @Sendable () throws -> Data
        do { archive = try makeArchive() }
        catch {
            lock.lock(); pending -= 1; lock.unlock()
            completion(.init(takeID: takeID, sourceSHA256: sourceSHA256, result: .needsRebuild(failure("Sealed waveform lease is unavailable; selected source requires cache rebuild"))))
            return
        }
        let lease = RecordingWaveformArchiveLease(archive)
        queue.async { [self] in
            let result: RecordingWaveformArchiveResult
            do {
                try writeHook()
                result = RecordingWaveformArchiveStore.persist(try lease.encodeAndRelease(), sourceSHA256: sourceSHA256, assetRoot: assetRoot)
            } catch { result = .needsRebuild(failure("Derived waveform archive failed; successfully sealed PCM remains valid")) }
            lease.release()
            lock.lock(); pending -= 1; lock.unlock()
            completion(.init(takeID: takeID, sourceSHA256: sourceSHA256, result: result))
        }
    }

    public func waitForBarrier(timeout: TimeInterval = 5) -> Bool {
        let finished = DispatchGroup(); finished.enter()
        queue.async { finished.leave() }
        return finished.wait(timeout: .now() + timeout) == .success
    }
    private func failure(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.WAVEFORM_CACHE_UNAVAILABLE", message: message)
    }
}
