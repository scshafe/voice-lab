import CryptoKit
import Darwin
import Foundation
import RecordingContracts

/// Canonical owned no-follow reader for bounded config and selected audio files.
public enum RecordingPrivateFile {
    public static func read(_ path: String, maximumBytes: Int = 1_048_576) throws -> Data {
        let lease = try openReadOnly(path, maximumBytes: Int64(maximumBytes))
        var result = Data()
        try lease.chunks { result.append($0) }
        return result
    }

    public static func openReadOnly(_ path: String, maximumBytes: Int64,
                                   diagnosticContext: RecordingDiagnosticContext? = nil,
                                   diagnosticSink: RecordingDiagnosticSink? = nil) throws -> Lease {
        let context = diagnosticSink == nil ? nil : (diagnosticContext ?? RecordingDiagnosticContext(traceID: UUID().uuidString.lowercased())).path(path)
        let span = RecordingDiagnosticSpan("private_file.open", context: context, sink: diagnosticSink)
        do {
            let lease = try Lease(path: path, maximumBytes: maximumBytes, diagnosticContext: span.childContext, diagnosticSink: diagnosticSink)
            span.finish(byteCount: lease.sizeBytes, device: Int64(lease.identity.device), inode: lease.identity.inode)
            return lease
        } catch { span.finish(.failure, error: error); throw error }
    }

    /// Immutable descriptor ownership; pread keeps independent readers race-free.
    public final class Lease: @unchecked Sendable {
        public let path: String
        public let sizeBytes: Int64
        private let descriptor: Int32
        fileprivate let identity: Identity
        private let diagnosticContext: RecordingDiagnosticContext?
        private let diagnosticSink: RecordingDiagnosticSink?

        fileprivate init(path: String, maximumBytes: Int64, diagnosticContext: RecordingDiagnosticContext?, diagnosticSink: RecordingDiagnosticSink?) throws {
            guard maximumBytes >= 0, path.hasPrefix("/"), let resolved = realpath(path, nil) else { throw invalid("resolve-input") }
            defer { free(resolved) }
            guard String(cString: resolved) == path else { throw invalid("noncanonical-input") }
            let components = path.split(separator: "/")
            guard let last = components.last else { throw invalid() }
            var parent = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
            guard parent >= 0 else { throw invalid() }
            defer { close(parent) }
            for component in components.dropLast() {
                let next = openat(parent, String(component), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                guard next >= 0 else { throw invalid("open-parent") }
                close(parent); parent = next
            }
            let opened = openat(parent, String(last), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
            guard opened >= 0 else { throw invalid("open-input") }
            do {
                var metadata = stat(), current = stat()
                guard fstat(opened, &metadata) == 0, lstat(path, &current) == 0,
                      metadata.st_mode & S_IFMT == S_IFREG, metadata.st_uid == getuid(),
                      metadata.st_mode & 0o077 == 0, metadata.st_nlink == 1,
                      metadata.st_size >= 0, metadata.st_size <= maximumBytes,
                      Identity(metadata) == Identity(current) else { throw invalid("input-identity") }
                self.identity = Identity(metadata)
                self.sizeBytes = metadata.st_size
                self.path = path
                self.descriptor = opened
                self.diagnosticContext = diagnosticContext
                self.diagnosticSink = diagnosticSink
            } catch { close(opened); throw error }
        }

        deinit { close(descriptor) }

        public func validateUnchanged() throws {
            let span = RecordingDiagnosticSpan("private_file.validate_identity", context: diagnosticContext, sink: diagnosticSink)
            do { try validateUnchangedIdentity(); span.finish(device: Int64(identity.device), inode: identity.inode) }
            catch { span.finish(.failure, error: error); throw error }
        }

        private func validateUnchangedIdentity() throws {
            var current = stat(), descriptorState = stat()
            guard fstat(descriptor, &descriptorState) == 0, lstat(path, &current) == 0,
                  identity == Identity(current), identity == Identity(descriptorState),
                  let resolved = realpath(path, nil) else { throw invalid("changed-identity") }
            defer { free(resolved) }
            guard String(cString: resolved) == path else { throw invalid("changed-alias") }
        }

        /// Bind the selected-source authorization to the descriptor we actually hold.
        public func validate(expectedIdentity: RecordingJSON, relativePath: String) throws {
            try validateUnchanged()
            guard let value = expectedIdentity.object,
                  let mtime = nanoseconds(identity.mtimeSeconds, identity.mtimeNanoseconds),
                  let ctime = nanoseconds(identity.ctimeSeconds, identity.ctimeNanoseconds),
                  Set(value.keys) == ["relative_path", "device", "inode", "size_bytes", "mode", "mtime_ns", "ctime_ns"],
                  value["relative_path"] == .string(relativePath),
                  value["device"]?.integer == Int64(identity.device),
                  value["inode"]?.integer == Int64(exactly: identity.inode),
                  value["size_bytes"]?.integer == identity.size,
                  value["mode"]?.integer == Int64(identity.mode),
                  value["mtime_ns"]?.integer == mtime,
                  value["ctime_ns"]?.integer == ctime else { throw invalid("authorized-identity") }
        }

        private func nanoseconds(_ seconds: Int, _ fraction: Int) -> Int64? {
            let whole = Int64(seconds).multipliedReportingOverflow(by: 1_000_000_000)
            let result = whole.partialValue.addingReportingOverflow(Int64(fraction))
            return whole.overflow || result.overflow ? nil : result.partialValue
        }

        /// Bounded streaming, with cancellation and complete descriptor/path checks.
        public func chunks(_ body: (Data) throws -> Void) throws {
            guard diagnosticSink != nil else { try readChunks(body); return }
            let span = RecordingDiagnosticSpan("private_file.read", context: diagnosticContext, sink: diagnosticSink)
            var bytesRead: Int64 = 0
            do {
                try readChunks { bytes in bytesRead += Int64(bytes.count); try body(bytes) }
                span.finish(byteCount: bytesRead, device: Int64(identity.device), inode: identity.inode)
            } catch { span.finish(error is CancellationError ? .cancelled : .failure, error: error, byteCount: bytesRead); throw error }
        }

        private func readChunks(_ body: (Data) throws -> Void) throws {
            try validateUnchanged()
            var offset: Int64 = 0
            var storage = [UInt8](repeating: 0, count: 65_536)
            while offset < sizeBytes {
                try Task.checkCancellation()
                let requested = min(storage.count, Int(sizeBytes - offset))
                let count = storage.withUnsafeMutableBytes { pread(descriptor, $0.baseAddress, requested, off_t(offset)) }
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw invalid() }
                try body(Data(storage.prefix(count)))
                offset += Int64(count)
            }
            try validateUnchanged()
        }

        public func sha256() throws -> String {
            let span = RecordingDiagnosticSpan("private_file.sha256", context: diagnosticContext, sink: diagnosticSink)
            do {
                var hasher = SHA256()
                try chunks { hasher.update(data: $0) }
                let digest = hasher.finalize().map { String(format: "%02x", $0) }.joined()
                span.finish(byteCount: sizeBytes, device: Int64(identity.device), inode: identity.inode)
                return digest
            } catch { span.finish(error is CancellationError ? .cancelled : .failure, error: error); throw error }
        }
    }

    fileprivate struct Identity: Equatable, Sendable {
        let device: Int32
        let inode: UInt64
        let size: Int64
        let mode: UInt16
        let owner: UInt32
        let links: UInt16
        let mtimeSeconds: Int
        let mtimeNanoseconds: Int
        let ctimeSeconds: Int
        let ctimeNanoseconds: Int
        init(_ value: stat) {
            device = value.st_dev; inode = value.st_ino; size = value.st_size
            mode = value.st_mode; owner = value.st_uid; links = value.st_nlink
            mtimeSeconds = value.st_mtimespec.tv_sec; mtimeNanoseconds = value.st_mtimespec.tv_nsec
            ctimeSeconds = value.st_ctimespec.tv_sec; ctimeNanoseconds = value.st_ctimespec.tv_nsec
        }
    }

    private static func invalid(_ boundary: String = "read-input") -> RecordingContractError {
        RecordingContractError(code: "RECORDING.SOURCE_CHANGED", message: "Input must be one unchanged bounded owned private regular file at a resolved path (\(boundary))", context: ["boundary": boundary])
    }
}
