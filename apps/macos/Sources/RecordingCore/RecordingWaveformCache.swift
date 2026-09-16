import CryptoKit
import Foundation
import RecordingContracts

/// @canonical recording_waveform_cache -- exact written PCM extrema at every retained resolution.
/// Fixed preallocation is under 7.3 MiB at ten minutes. Only bounded copies cross to renderers;
/// callbacks never call this cache and render work never executes while its lock is held.
public final class RecordingWaveformCache: @unchecked Sendable {
    public static let baseBucketFrames: Int64 = 64
    public static let maximumFrames: Int64 = 28_800_000
    public static let maximumSnapshotBuckets = 4096
    public static let maximumArchiveBytes = 3_600_084
    private let takeID: String
    private let generation: String
    private let limit: Int64
    private let lock = NSLock()
    private var levels: [[RecordingWaveformExtrema]] = []
    private var written: Int64 = 0
    private var completeBaseCount = 0
    private var partialCount: Int64 = 0
    private var partial = RecordingWaveformExtrema(minimum: .max, maximum: .min)
    private var clock: RecordingWaveformSampleClock?
    public let allocatedExtremaBytes: Int

    public init(takeID: String, processGeneration: String, maximumFrames: Int64 = maximumFrames) throws {
        guard !takeID.isEmpty, !processGeneration.isEmpty, takeID.utf8.count <= 128, processGeneration.utf8.count <= 128,
              (1...Self.maximumFrames).contains(maximumFrames) else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Waveform cache requires bounded take, generation and frame identities")
        }
        self.takeID = takeID; self.generation = processGeneration; self.limit = maximumFrames
        var count = Int((maximumFrames + Self.baseBucketFrames - 1) / Self.baseBucketFrames)
        var allocation = 0
        while true {
            levels.append([RecordingWaveformExtrema](repeating: .init(minimum: 0, maximum: 0), count: count))
            allocation += count * MemoryLayout<RecordingWaveformExtrema>.stride
            if count == 1 { break }
            count = (count + 1) / 2
        }
        self.allocatedExtremaBytes = allocation
    }

    /// Append at most 4096 frames AFTER their native file-write acknowledgment.
    /// Reject discontinuity, oversized calls and a changed source clock rather than invent samples.
    @discardableResult public func append(pcm: UnsafeRawBufferPointer, startFrame: Int64, sampleClock: RecordingWaveformSampleClock?) throws -> RecordingWaveformExtrema {
        let count = pcm.count / 3
        guard pcm.count % 3 == 0, (1...4096).contains(count) else { throw invalid("Waveform append requires 1...4096 complete mono24-bit frames") }
        lock.lock(); defer { lock.unlock() }
        guard startFrame == written, written + Int64(count) <= limit else { throw invalid("Waveform source frames are discontinuous or exceed their take bound") }
        if let sampleClock {
            guard sampleClock.anchorFrame <= startFrame, clock == nil || clock == sampleClock else { throw invalid("Waveform source clock changed") }
            clock = sampleClock
        }
        var chunk = RecordingWaveformExtrema(minimum: .max, maximum: .min)
        for index in stride(from: 0, to: pcm.count, by: 3) {
            let unsigned = UInt32(pcm[index]) | UInt32(pcm[index + 1]) << 8 | UInt32(pcm[index + 2]) << 16
            let sample = unsigned & 0x800000 == 0 ? Int32(unsigned) : Int32(Int64(unsigned) - 0x1000000)
            chunk = .init(minimum: min(chunk.minimum, sample), maximum: max(chunk.maximum, sample))
            partial = .init(minimum: min(partial.minimum, sample), maximum: max(partial.maximum, sample))
            partialCount += 1
            written += 1
            if partialCount == Self.baseBucketFrames {
                levels[0][completeBaseCount] = partial
                var completed = completeBaseCount + 1
                var level = 1
                while completed % 2 == 0, level < levels.count {
                    let destination = completed / 2 - 1
                    levels[level][destination] = .merge(levels[level - 1][destination * 2], levels[level - 1][destination * 2 + 1])
                    completed /= 2; level += 1
                }
                completeBaseCount += 1
                partialCount = 0
                partial = .init(minimum: .max, maximum: .min)
            }
        }
        return chunk
    }

    public func snapshot(range: Range<Int64>? = nil, maximumBuckets: Int = maximumSnapshotBuckets) throws -> RecordingWaveformSnapshot {
        guard (1...Self.maximumSnapshotBuckets).contains(maximumBuckets) else { throw invalid("Waveform snapshots require a 1...4096 bucket bound") }
        lock.lock(); defer { lock.unlock() }
        let lower = max(0, min(written, range?.lowerBound ?? 0))
        let upper = max(lower, min(written, range?.upperBound ?? written))
        var width = Self.baseBucketFrames
        while upper > lower, (upper + width - 1) / width - lower / width > maximumBuckets {
            width *= 2
        }
        let first = lower / width * width
        var result: [RecordingWaveformExtrema] = []
        if upper > lower {
            result.reserveCapacity(Int((upper - first + width - 1) / width))
            for bucketStart in stride(from: first, to: upper, by: Int(width)) {
                let bucketEnd = min(bucketStart + width, written)
                let baseStart = Int(bucketStart / Self.baseBucketFrames)
                let baseEnd = Int((bucketEnd + Self.baseBucketFrames - 1) / Self.baseBucketFrames)
                result.append(extrema(baseStart..<baseEnd))
            }
        }
        // Last snapshot bucket covers its actual included source bucket, never a made-up partial range.
        let end = result.isEmpty ? first : min(first + Int64(result.count) * width, written)
        return RecordingWaveformSnapshot(takeID: takeID, processGeneration: generation, firstFrame: first,
            endFrame: end, writtenFrames: written, bucketWidthFrames: width, buckets: result, sampleClock: clock)
    }

    /// Select a coarser retained resolution if complete framed JSON would exceed 256 KiB.
    /// The pyramid keeps every source peak; transport sizing never discards raw samples.
    public func framedSnapshot(range: Range<Int64>? = nil, meta: RecordingJSON) throws -> (snapshot: RecordingWaveformSnapshot, frame: Data) {
        var capacity = Self.maximumSnapshotBuckets
        while true {
            let snapshot = try snapshot(range: range, maximumBuckets: capacity)
            let payload = try RecordingJSON.object(["data": snapshot.wireData, "error": .null, "meta": meta]).transportData()
            if payload.count <= RecordingFrameCodec.telemetryMaximumBytes {
                return (snapshot, try RecordingFrameCodec.encode(payload, maximumBytes: RecordingFrameCodec.telemetryMaximumBytes))
            }
            guard capacity > 1 else { throw invalid("Waveform identities exceed the telemetry byte bound") }
            capacity = max(1, capacity / 2)
        }
    }

    private func extrema(_ range: Range<Int>) -> RecordingWaveformExtrema {
        var index = range.lowerBound
        var result = RecordingWaveformExtrema(minimum: .max, maximum: .min)
        while index < range.upperBound {
            if index == completeBaseCount {
                result = .merge(result, partial); index += 1; continue
            }
            var level = 0
            var size = 1
            while level + 1 < levels.count, index % (size * 2) == 0,
                  index + size * 2 <= min(range.upperBound, completeBaseCount) {
                level += 1; size *= 2
            }
            result = .merge(result, levels[level][index / size]); index += size
        }
        return result
    }

    private func invalid(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: message)
    }
}

extension RecordingWaveformCache {
    /// Version2 binds both the original digest and the complete extrema payload checksum.
    /// Legacy version1 caches are explicit rebuild misses. Host timestamps are omitted:
    /// a future process/boot cannot treat historical capture clocks as current age evidence.
    public func archive(sourceSHA256: String, finalWrittenFrames: Int64) throws -> Data {
        let digest = try Self.digestBytes(sourceSHA256)
        lock.lock(); defer { lock.unlock() }
        guard finalWrittenFrames > 0, finalWrittenFrames == written else { throw invalid("Waveform archive must agree with the final sealed source frame count") }
        var data = Data("CVLWAV02".utf8)
        func append(_ value: UInt64, bytes: Int) { for shift in stride(from: 0, to: bytes * 8, by: 8) { data.append(UInt8(truncatingIfNeeded: value >> shift)) } }
        append(UInt64(written), bytes: 8)
        append(UInt64(Self.baseBucketFrames), bytes: 4)
        data.append(digest)
        for index in 0..<completeBaseCount {
            append(UInt64(UInt32(bitPattern: levels[0][index].minimum)), bytes: 4)
            append(UInt64(UInt32(bitPattern: levels[0][index].maximum)), bytes: 4)
        }
        if partialCount > 0 {
            append(UInt64(UInt32(bitPattern: partial.minimum)), bytes: 4)
            append(UInt64(UInt32(bitPattern: partial.maximum)), bytes: 4)
        }
        data.append(contentsOf: SHA256.hash(data: data))
        return data
    }

    /// Decode only bounded source-bound extrema, never historical PCM or stored host timestamps.
    public convenience init(archive: Data, expectedSourceSHA256: String, takeID: String, processGeneration: String) throws {
        func invalidArchive() -> RecordingContractError { RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Waveform archive is malformed or differs from its sealed source identity") }
        guard archive.count >= 84, archive.count <= Self.maximumArchiveBytes,
              archive.prefix(8) == Data("CVLWAV02".utf8),
              archive.suffix(32) == Data(SHA256.hash(data: archive.dropLast(32))) else { throw invalidArchive() }
        func unsigned(_ offset: Int, bytes: Int) -> UInt64 {
            (0..<bytes).reduce(0) { $0 | UInt64(archive[offset + $1]) << ($1 * 8) }
        }
        let frameCount = unsigned(8, bytes: 8)
        guard (1...UInt64(Self.maximumFrames)).contains(frameCount), unsigned(16, bytes: 4) == Self.baseBucketFrames,
              archive.subdata(in: 20..<52) == (try Self.digestBytes(expectedSourceSHA256)),
              archive.count == 84 + Int((frameCount + 63) / 64) * 8 else { throw invalidArchive() }
        try self.init(takeID: takeID, processGeneration: processGeneration, maximumFrames: Int64(frameCount))
        let count = Int((frameCount + 63) / 64)
        for index in 0..<count {
            let minimum = Int32(bitPattern: UInt32(unsigned(52 + index * 8, bytes: 4)))
            let maximum = Int32(bitPattern: UInt32(unsigned(56 + index * 8, bytes: 4)))
            guard minimum >= -8_388_608, maximum <= 8_388_607, minimum <= maximum else { throw invalidArchive() }
            let extrema = RecordingWaveformExtrema(minimum: minimum, maximum: maximum)
            if index == count - 1, frameCount % 64 != 0 {
                partial = extrema; partialCount = Int64(frameCount % 64)
            } else {
                levels[0][index] = extrema
                completeBaseCount += 1
                var completed = completeBaseCount, level = 1
                while completed % 2 == 0, level < levels.count {
                    let destination = completed / 2 - 1
                    levels[level][destination] = .merge(levels[level - 1][destination * 2], levels[level - 1][destination * 2 + 1])
                    completed /= 2; level += 1
                }
            }
        }
        written = Int64(frameCount)
    }

    private static func digestBytes(_ digest: String) throws -> Data {
        guard digest.utf8.count == 64, digest.allSatisfy({ ("0"..."9").contains($0) || ("a"..."f").contains($0) }) else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Waveform archive requires a lowercase sealed source SHA-256 identity")
        }
        let characters = Array(digest)
        return Data(stride(from: 0, to: 64, by: 2).map { UInt8(String(characters[$0...($0 + 1)]), radix: 16)! })
    }
}
