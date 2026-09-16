import CaptureBuffer
import CryptoKit
import Darwin
import Foundation
import RecordingContracts

public enum NativeCaptureWritePoint: Sendable {
    case initialHeader, pcm, finalHeader, fileSync, publication, directorySync, recoveryWrite, recoverySync
}

/// Fault hooks execute on the writer/control side only; production hook is empty.
public typealias NativeCaptureWriteHook = @Sendable (NativeCaptureWritePoint) throws -> Void

/// Canonical native filesystem writer; all paths are descriptor-relative and no-follow.
/// Python subsequently independently verifies and canonically persists returned evidence.
private final class NativeCaptureDescriptor {
    let value: Int32
    init(_ value: Int32) { self.value = value }
    deinit { Darwin.close(value) }
}

final class NativeCaptureWriter: @unchecked Sendable {
    private let directoryOwner: NativeCaptureDescriptor
    private let descriptorOwner: NativeCaptureDescriptor
    private var directory: Int32 { directoryOwner.value }
    private var descriptor: Int32 { descriptorOwner.value }
    private let initialIdentity: stat
    private let hook: NativeCaptureWriteHook
    private let request: NativeCaptureRequest
    private let hostSecondsPerTick: Double
    let waveform: RecordingWaveformCache
    private var pcmBytes: UInt64 = 0
    private var published = false
    private var buffer = [UInt8](repeating: 0, count: 4096 * 3)

    init(request: NativeCaptureRequest, hook: @escaping NativeCaptureWriteHook,
         hostSecondsPerTick: Double = NativeCaptureClock.system.hostSecondsPerTick) throws {
        self.request = request
        self.hook = hook
        self.hostSecondsPerTick = hostSecondsPerTick
        self.waveform = try RecordingWaveformCache(takeID: request.takeID, processGeneration: request.processGeneration, maximumFrames: Int64(request.maximumFrames))
        var opened = try NativeCaptureStorage.openPrivateRoot(request.assetRoot)
        do {
            try NativeCaptureStorage.requireAvailable(descriptor: opened, maximumFrames: request.maximumFrames)
            for component in ["tmp", "recording-takes", request.takeID] {
                let next = Darwin.openat(opened, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                guard next >= 0 else { throw NativeCaptureFailure.system("open reserved take directory") }
                do { try Self.requirePrivateDirectory(next) }
                catch { Darwin.close(next); throw error }
                Darwin.close(opened)
                opened = next
            }
            var existing = stat()
            if fstatat(opened, "original.wav", &existing, AT_SYMLINK_NOFOLLOW) == 0 || errno != ENOENT {
                throw NativeCaptureFailure("RECORDING.SOURCE_CHANGED", "Reserved original already exists or cannot be checked")
            }
            let output = Darwin.openat(opened, "capture.partial.wav", O_RDWR | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, mode_t(0o600))
            guard output >= 0 else { throw NativeCaptureFailure.system("exclusively create capture partial") }
            var metadata = stat()
            guard fchmod(output, 0o600) == 0, fstat(output, &metadata) == 0,
                  metadata.st_mode & S_IFMT == S_IFREG, metadata.st_nlink == 1,
                  metadata.st_uid == geteuid(), metadata.st_mode & 0o777 == 0o600 else {
                Darwin.close(output)
                throw NativeCaptureFailure("RECORDING.SOURCE_CHANGED", "Capture partial is not one private regular file")
            }
            directoryOwner = NativeCaptureDescriptor(opened)
            descriptorOwner = NativeCaptureDescriptor(output)
            initialIdentity = metadata
        } catch {
            Darwin.close(opened)
            throw error
        }
        do {
            try hook(.initialHeader)
            try writeAll(Self.wavHeader(frames: 0))
        } catch {
            // A partial always survives initialization failure. Independent ARC
            // descriptor owners clean up even if this throwing initializer fails.
            throw error
        }
    }

    private static func requirePrivateDirectory(_ fd: Int32) throws {
        var metadata = stat()
        guard fstat(fd, &metadata) == 0, metadata.st_mode & S_IFMT == S_IFDIR,
              metadata.st_mode & 0o077 == 0, metadata.st_uid == geteuid() else {
            throw NativeCaptureFailure("RECORDING.SOURCE_CHANGED", "Capture directory must be owned and private")
        }
    }

    private func writeAll(_ data: Data) throws {
        try data.withUnsafeBytes { bytes in try writeAll(bytes.baseAddress!, count: bytes.count) }
    }

    private func writeAll(_ pointer: UnsafeRawPointer, count: Int) throws {
        var offset = 0
        while offset < count {
            let result = Darwin.write(descriptor, pointer.advanced(by: offset), count - offset)
            if result < 0, errno == EINTR { continue }
            guard result > 0 else { throw NativeCaptureFailure.system("write PCM bytes") }
            offset += result
        }
    }

    static func wavHeader(frames: UInt64) -> Data {
        let pcmSize = UInt32(frames * 3)
        var data = Data()
        func string(_ text: String) { data.append(contentsOf: text.utf8) }
        func u16(_ value: UInt16) { data.append(UInt8(truncatingIfNeeded: value)); data.append(UInt8(truncatingIfNeeded: value >> 8)) }
        func u32(_ value: UInt32) {
            for shift in stride(from: 0, to: 32, by: 8) { data.append(UInt8(truncatingIfNeeded: value >> shift)) }
        }
        string("RIFF"); u32(pcmSize + 36); string("WAVEfmt "); u32(16)
        u16(1); u16(1); u32(48_000); u32(144_000); u16(3); u16(24)
        string("data"); u32(pcmSize)
        return data
    }

    /// Drain a bounded number of raw chunks so shutdown remains observable.
    /// Extrema are derived from the exact bytes written, never from UI scheduling.
    func drain(_ context: OpaquePointer, maximumChunks: Int = 32) throws {
        for _ in 0..<maximumChunks {
            let startFrame = cvl_capture_snapshot(context).dequeued_frames
            let frames = buffer.withUnsafeMutableBytes { bytes in
                cvl_capture_read_pcm(context, bytes.baseAddress, 4096)
            }
            if frames == 0 { return }
            try hook(.pcm)
            try buffer.withUnsafeBytes { bytes in try writeAll(bytes.baseAddress!, count: Int(frames) * 3) }
            pcmBytes += UInt64(frames) * 3
            guard cvl_capture_acknowledge_written(context, frames) else {
                throw NativeCaptureFailure("RECORDING.WRITE_FAILED", "Native writer frame acknowledgment was rejected")
            }
            let snapshot = cvl_capture_snapshot(context)
            guard snapshot.first_host_time <= UInt64(Int64.max) else {
                throw NativeCaptureFailure("RECORDING.CONTINUITY_FAILED", "Waveform host timestamp exceeds its signed transport bound")
            }
            let sampleClock = snapshot.first_host_time_valid ? try RecordingWaveformSampleClock(
                anchorFrame: 0, hostTimeTicks: Int64(snapshot.first_host_time), secondsPerTick: hostSecondsPerTick
            ) : nil
            let extrema = try buffer.withUnsafeBytes { bytes in
                try waveform.append(pcm: UnsafeRawBufferPointer(rebasing: bytes[..<(Int(frames) * 3)]), startFrame: Int64(startFrame), sampleClock: sampleClock)
            }
            let hostOffset = (Double(startFrame) / 48_000 / hostSecondsPerTick).rounded()
            guard hostOffset.isFinite, hostOffset >= 0,
                  hostOffset < Double(UInt64.max - snapshot.first_host_time) else {
                throw NativeCaptureFailure("RECORDING.CONTINUITY_FAILED", "Waveform source host timestamp is outside its clock range")
            }
            var envelope = CVLCaptureEnvelope(generation: request.bufferGeneration, start_frame: startFrame,
                frame_count: frames, minimum_pcm: extrema.minimum, maximum_pcm: extrema.maximum,
                sample_time: snapshot.first_sample_time + Double(startFrame),
                host_time: snapshot.first_host_time + UInt64(hostOffset), host_time_valid: snapshot.first_host_time_valid)
            _ = cvl_capture_publish_envelope(context, &envelope)
        }
    }

    private func validateIdentity(expectedSize: UInt64, name: String) throws -> stat {
        var descriptorState = stat(), pathState = stat()
        guard fstat(descriptor, &descriptorState) == 0,
              fstatat(directory, name, &pathState, AT_SYMLINK_NOFOLLOW) == 0,
              descriptorState.st_dev == initialIdentity.st_dev,
              descriptorState.st_ino == initialIdentity.st_ino,
              descriptorState.st_mode & S_IFMT == S_IFREG,
              descriptorState.st_nlink == 1, descriptorState.st_mode & 0o777 == 0o600,
              descriptorState.st_size == expectedSize,
              pathState.st_dev == descriptorState.st_dev, pathState.st_ino == descriptorState.st_ino,
              pathState.st_size == descriptorState.st_size else {
            throw NativeCaptureFailure("RECORDING.SOURCE_CHANGED", "Capture file identity, permissions, or size changed")
        }
        return descriptorState
    }

    /// Seal only exact drained PCM. Digest reads the complete final WAV through its original fd.
    func seal(snapshot: CVLCaptureSnapshot) throws -> (sha256: String, size: UInt64) {
        guard snapshot.queue_disposed, snapshot.error == CVL_CAPTURE_OK,
              snapshot.received_frames > 0, snapshot.received_frames == snapshot.written_frames,
              snapshot.dequeued_frames == snapshot.written_frames,
              pcmBytes == snapshot.written_frames * 3,
              try waveform.snapshot(maximumBuckets: 1).writtenFrames == Int64(snapshot.written_frames), snapshot.callback_count > 0,
              abs(snapshot.next_sample_time - snapshot.first_sample_time - Double(snapshot.written_frames)) <= 0.000001 else {
            throw NativeCaptureFailure("RECORDING.CONTINUITY_FAILED", "Sealing requires complete, continuous, actually written PCM")
        }
        try hook(.finalHeader)
        guard lseek(descriptor, 0, SEEK_SET) == 0 else { throw NativeCaptureFailure.system("seek final WAV header") }
        try writeAll(Self.wavHeader(frames: snapshot.written_frames))
        try hook(.fileSync)
        guard fsync(descriptor) == 0 else { throw NativeCaptureFailure.system("sync final WAV") }
        let size = 44 + pcmBytes
        let before = try validateIdentity(expectedSize: size, name: "capture.partial.wav")
        guard lseek(descriptor, 0, SEEK_SET) == 0 else { throw NativeCaptureFailure.system("seek final WAV digest") }
        var hash = SHA256()
        var digestBuffer = [UInt8](repeating: 0, count: 65536)
        var hashed: UInt64 = 0
        while true {
            let count = digestBuffer.withUnsafeMutableBytes { Darwin.read(descriptor, $0.baseAddress, $0.count) }
            if count < 0, errno == EINTR { continue }
            guard count >= 0 else { throw NativeCaptureFailure.system("read final WAV digest") }
            if count == 0 { break }
            hash.update(data: Data(digestBuffer.prefix(count)))
            hashed += UInt64(count)
        }
        let after = try validateIdentity(expectedSize: size, name: "capture.partial.wav")
        guard hashed == size, before.st_mtimespec.tv_sec == after.st_mtimespec.tv_sec,
              before.st_mtimespec.tv_nsec == after.st_mtimespec.tv_nsec,
              before.st_ctimespec.tv_sec == after.st_ctimespec.tv_sec,
              before.st_ctimespec.tv_nsec == after.st_ctimespec.tv_nsec else {
            throw NativeCaptureFailure("RECORDING.SOURCE_CHANGED", "Capture changed during digest verification")
        }
        let digest = hash.finalize().map { String(format: "%02x", $0) }.joined()
        try hook(.publication)
        guard renameatx_np(directory, "capture.partial.wav", directory, "original.wav", UInt32(RENAME_EXCL)) == 0 else {
            throw NativeCaptureFailure.system("publish original without replacement")
        }
        published = true
        do {
            _ = try validateIdentity(expectedSize: size, name: "original.wav")
            try hook(.directorySync)
            guard fsync(directory) == 0 else { throw NativeCaptureFailure.system("sync original publication") }
        } catch {
            // Return our original to its recoverable partial name if publication
            // cannot be proven durable. Never replace a newly appeared partial.
            do {
                _ = try validateIdentity(expectedSize: size, name: "original.wav")
                guard renameatx_np(directory, "original.wav", directory, "capture.partial.wav", UInt32(RENAME_EXCL)) == 0 else {
                    throw NativeCaptureFailure.system("restore failed publication to partial")
                }
                published = false
                guard fsync(directory) == 0 else { throw NativeCaptureFailure.system("sync restored capture partial") }
            } catch let restoration {
                throw NativeCaptureFailure("RECORDING.WRITE_FAILED", "Native publication failed and partial restoration was not completed",
                                           details: [String(describing: type(of: restoration))])
            }
            throw error
        }
        return (digest, size)
    }

    /// Recovery JSON is deliberately transport serialization, never authoritative canonical evidence.
    func persistRecovery(_ recovery: RecordingJSON) throws {
        let temporary = ".native-recovery-\(UUID().uuidString.lowercased()).tmp"
        let output = Darwin.openat(directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, mode_t(0o600))
        guard output >= 0 else { throw NativeCaptureFailure.system("create emergency recovery receipt") }
        defer { Darwin.close(output) }
        try hook(.recoveryWrite)
        let data = try recovery.transportData()
        guard data.count <= 1_048_576 else { throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Native recovery receipt exceeds its byte bound") }
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let count = Darwin.write(output, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if count < 0, errno == EINTR { continue }
                guard count > 0 else { throw NativeCaptureFailure.system("write emergency recovery receipt") }
                offset += count
            }
        }
        try hook(.recoverySync)
        guard fsync(output) == 0 else { throw NativeCaptureFailure.system("sync emergency recovery receipt") }
        guard renameatx_np(directory, temporary, directory, "native-recovery.json", UInt32(RENAME_EXCL)) == 0 else {
            throw NativeCaptureFailure.system("publish emergency recovery receipt without replacement")
        }
        guard fsync(directory) == 0 else { throw NativeCaptureFailure.system("sync emergency recovery publication") }
    }
}
