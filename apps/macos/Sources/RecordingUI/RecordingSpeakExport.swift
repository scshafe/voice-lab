import CryptoKit
import Darwin
import Foundation
import RecordingContracts
import RecordingCore

/// @canonical speak_export -- Save As for a Speak clip.
/// A byte copy of a checksum-bound clip to a place the operator chose. The clip is read once
/// through the owned no-follow reader, hashed as it is copied, and the copy replaces the
/// destination only when the bytes are exactly the ones Speak wrote. Never a hard link: the
/// clip keeps one link, which its playback requires.
enum RecordingSpeakExport {
    static let changedMessage = "The clip changed after Speak wrote it, so it was not saved"
    static let insideLibraryMessage = "Save the clip outside the recording library"

    /// Throws: RECORDING.INVALID_MESSAGE for a destination that is not an absolute file path
    /// or lies inside `assetRoot`; RECORDING.SOURCE_CHANGED when the clip is missing, not an
    /// owned private single-link file, larger than `maximumBytes`, changes while it is read, or
    /// no longer hashes to `sha256` (nothing is left at the destination); RECORDING.WRITE_FAILED
    /// when the destination folder cannot be written (the temporary file is removed).
    static func copy(assetRoot: URL, relativePath: String, sha256: String, maximumBytes: Int64, to destination: URL) throws {
        let name = destination.lastPathComponent
        guard destination.isFileURL, destination.path.hasPrefix("/"), !name.isEmpty, name != "/", name != ".", name != "..",
              !name.contains("\0") else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Choose a file to save the clip to")
        }
        let folder = try resolved(destination.deletingLastPathComponent().path, name: name)
        // The clip lives under the library, so a library that cannot be resolved has no clip.
        guard let library = try? resolved(assetRoot.path, name: name) else { throw changed(nil) }
        guard folder != library, !folder.hasPrefix(library.hasSuffix("/") ? library : library + "/") else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: insideLibraryMessage)
        }
        let lease: RecordingPrivateFile.Lease
        do {
            lease = try RecordingPrivateFile.openReadOnly(assetRoot.appendingPathComponent(relativePath).path, maximumBytes: maximumBytes)
        } catch { throw changed(error) }

        let directory = open(folder, O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard directory >= 0 else { throw writeFailed(name) }
        defer { close(directory) }
        let temporary = "." + name + ".speak-" + UUID().uuidString.lowercased() + ".tmp"
        let descriptor = openat(directory, temporary, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC, 0o644)
        guard descriptor >= 0 else { throw writeFailed(name) }
        var replaced = false
        defer {
            close(descriptor)
            if !replaced { unlinkat(directory, temporary, 0) }
        }
        var hasher = SHA256()
        var writeError: RecordingContractError?
        do {
            try lease.chunks { chunk in
                hasher.update(data: chunk)
                do { try write(chunk, to: descriptor, name: name) } catch let error as RecordingContractError { writeError = error; throw error }
            }
        } catch {
            throw writeError ?? changed(error)
        }
        guard hasher.finalize().map({ String(format: "%02x", $0) }).joined() == sha256 else { throw changed(nil) }
        guard fsync(descriptor) == 0 else { throw writeFailed(name) }
        guard renameat(directory, temporary, directory, name) == 0 else { throw writeFailed(name) }
        replaced = true
        // A file system without directory flushes (EINVAL, ENOTSUP) still holds the renamed copy.
        guard fsync(directory) == 0 || errno == EINVAL || errno == ENOTSUP else { throw writeFailed(name) }
    }

    private static func resolved(_ path: String, name: String) throws -> String {
        guard let pointer = realpath(path, nil) else { throw writeFailed(name) }
        defer { free(pointer) }
        return String(cString: pointer)
    }

    private static func write(_ chunk: Data, to descriptor: Int32, name: String) throws {
        try chunk.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count {
                let written = Darwin.write(descriptor, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if written < 0 && errno == EINTR { continue }
                guard written > 0 else { throw writeFailed(name) }
                offset += written
            }
        }
    }

    private static func changed(_ cause: (any Error)?) -> RecordingContractError {
        var context: [String: String] = [:]
        if let cause = cause as? RecordingContractError { context = cause.context }
        return RecordingContractError(code: "RECORDING.SOURCE_CHANGED", message: changedMessage, context: context)
    }

    private static func writeFailed(_ name: String) -> RecordingContractError {
        let reason = String(cString: strerror(errno))
        return RecordingContractError(code: "RECORDING.WRITE_FAILED", message: "The clip could not be saved as \(name): \(reason)")
    }
}
