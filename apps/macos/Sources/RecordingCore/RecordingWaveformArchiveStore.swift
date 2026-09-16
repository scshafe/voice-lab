import Darwin
import Foundation
import RecordingContracts

public enum RecordingWaveformArchiveResult: Sendable {
    case stored(URL)
    case needsRebuild(RecordingContractError)
}

/// Rebuildable derived persistence runs independently after successful original sealing.
/// Failure returns an explicit cache-rebuild state; it never changes capture receipts or raw files.
public enum RecordingWaveformArchiveStore {
    public static func persist(_ archive: Data, sourceSHA256: String, assetRoot: URL,
                               writeHook: () throws -> Void = {}) -> RecordingWaveformArchiveResult {
        var directory: Int32 = -1
        var output: Int32 = -1
        var temporary: String?
        defer {
            if output >= 0 { close(output) }
            if let temporary, directory >= 0 { unlinkat(directory, temporary, 0) }
            if directory >= 0 { close(directory) }
        }
        do {
            // Decode the bound binary format before any filesystem publication.
            _ = try RecordingWaveformCache(archive: archive, expectedSourceSHA256: sourceSHA256,
                takeID: "derived-cache", processGeneration: "historical")
            guard let resolved = realpath(assetRoot.path, nil) else { throw failure("Asset root is unavailable") }
            defer { free(resolved) }
            guard String(cString: resolved) == assetRoot.path else { throw failure("Asset root must be resolved") }
            directory = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
            guard directory >= 0 else { throw failure("Cannot open cache root") }
            for component in assetRoot.path.split(separator: "/") {
                let next = openat(directory, String(component), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                guard next >= 0 else { throw failure("Asset root contains an unsafe directory") }
                close(directory); directory = next
            }
            try requirePrivate(directory)
            for component in ["derived", "recording-waveforms"] {
                guard mkdirat(directory, component, 0o700) == 0 || errno == EEXIST else { throw failure("Cannot create derived cache directory") }
                let next = openat(directory, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                guard next >= 0 else { throw failure("Derived cache directory is unsafe") }
                close(directory); directory = next
                try requirePrivate(directory)
            }
            let name = sourceSHA256 + "-v2.cvlwave"
            let staged = ".waveform-" + UUID().uuidString.lowercased() + ".tmp"
            output = openat(directory, staged, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
            guard output >= 0 else { throw failure("Cannot stage derived waveform cache") }
            temporary = staged
            try writeHook()
            try archive.withUnsafeBytes { bytes in
                var offset = 0
                while offset < bytes.count {
                    let count = Darwin.write(output, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                    if count < 0, errno == EINTR { continue }
                    guard count > 0 else { throw failure("Cannot write derived waveform cache") }
                    offset += count
                }
            }
            guard fsync(output) == 0 else { throw failure("Cannot sync derived waveform cache") }
            guard renameatx_np(directory, staged, directory, name, UInt32(RENAME_EXCL)) == 0 else {
                if errno == EEXIST {
                    let existing = assetRoot.appendingPathComponent("derived/recording-waveforms/" + name)
                    if try RecordingPrivateFile.read(existing.path, maximumBytes: RecordingWaveformCache.maximumArchiveBytes) == archive { return .stored(existing) }
                }
                // Never replace uncertain evidence; a selected-source rebuild may resolve it.
                throw failure("Derived waveform cache publication is unavailable or differs from existing bytes")
            }
            temporary = nil
            guard fsync(directory) == 0 else { throw failure("Cannot sync derived waveform directory") }
            return .stored(assetRoot.appendingPathComponent("derived/recording-waveforms/" + name))
        } catch let error as RecordingContractError { return .needsRebuild(error) }
        catch { return .needsRebuild(failure("Derived cache write failed; sealed original remains valid")) }
    }

    public static func load(sourceSHA256: String, assetRoot: URL, takeID: String, processGeneration: String) throws -> RecordingWaveformCache {
        guard sourceSHA256.utf8.count == 64, sourceSHA256.allSatisfy({ ("0"..."9").contains($0) || ("a"..."f").contains($0) }) else { throw failure("Invalid waveform source identity") }
        let path = assetRoot.appendingPathComponent("derived/recording-waveforms/" + sourceSHA256 + "-v2.cvlwave")
        do {
            let data = try RecordingPrivateFile.read(path.path, maximumBytes: RecordingWaveformCache.maximumArchiveBytes)
            return try RecordingWaveformCache(archive: data, expectedSourceSHA256: sourceSHA256, takeID: takeID, processGeneration: processGeneration)
        } catch {
            throw failure("Selected-source waveform cache is missing or invalid and needs rebuild; original audio remains valid")
        }
    }
    private static func requirePrivate(_ descriptor: Int32) throws {
        var metadata = stat()
        guard fstat(descriptor, &metadata) == 0, metadata.st_mode & S_IFMT == S_IFDIR,
              metadata.st_mode & 0o077 == 0, metadata.st_uid == geteuid() else { throw failure("Derived cache directory must be private and owned") }
    }
    private static func failure(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.WAVEFORM_CACHE_UNAVAILABLE", message: message)
    }
}
