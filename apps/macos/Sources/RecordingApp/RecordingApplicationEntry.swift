import AppKit
import Darwin
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI

/// Production executable entry. The explicit synthetic gate exercises its configured
/// peers without creating a SwiftUI window, requesting input, or starting output.
@main
@MainActor
enum RecordingApplicationEntry {
    static func main() {
        let entered = DispatchTime.now().uptimeNanoseconds
        let flags = ["--verification-report", "--verification-exit", "--synthetic-playback"]
        guard CommandLine.arguments.contains(where: { flags.contains($0) }) else {
            RecordingApp.main()
            return
        }
        do {
            let report = try verificationPath(CommandLine.arguments)
            // AppKit owns the ordinary main run loop; no Scene or window is created.
            NSApplication.shared.setActivationPolicy(.prohibited)
            Task { @MainActor in
                let code = await verify(report: report, entered: entered)
                Darwin.exit(code)
            }
            NSApplication.shared.run()
        } catch {
            FileHandle.standardError.write(Data("Synthetic application verification arguments are invalid: \(error)\n".utf8))
            Darwin.exit(64)
        }
    }

    private static func verificationPath(_ arguments: [String]) throws -> String {
        let required = ["--verification-report", "--verification-exit", "--synthetic-playback"]
        guard required.allSatisfy({ flag in arguments.filter { $0 == flag }.count == 1 }),
              let position = arguments.firstIndex(of: "--verification-report"), position + 1 < arguments.count,
              arguments[position + 1].hasPrefix("/") else { throw invalid("All explicit verification flags and one absolute report path are required") }
        let path = arguments[position + 1]
        try RecordingVerificationReportFile.validate(path)
        return path
    }

    private static func verify(report: String, entered: UInt64) async -> Int32 {
        let session = RecordingApplicationSession()
        var result: [String: RecordingJSON] = [
            "schema_version": .string("cole-voice-lab.recording-application-verification/v1"),
            "launch_entry_uptime_ns": .integer(Int64(entered)),
            "timing_scope": .string("process-entry-to-useful-configured-model; excludes dynamic loader and display"),
            "shown_window": .boolean(false), "activated_application": .boolean(false),
            "microphone_opened": .boolean(false), "playback_started": .boolean(false),
            "recording_started": .boolean(false), "synthetic_playback": .boolean(true),
            "installed_app_restarted": .boolean(false)
        ]
        var passed = false
        do {
            let configurationURL = try RecordingApplicationConfiguration.configurationURL()
            let configuration = try await Task.detached(priority: .userInitiated) {
                try RecordingApplicationConfiguration.load(privateFile: configurationURL)
            }.value
            guard configuration.recorder.arguments.contains("--synthetic-pcm") else {
                throw invalid("Verification requires an explicitly synthetic helper configuration")
            }
            result["configuration_loaded_uptime_ns"] = .integer(Int64(DispatchTime.now().uptimeNanoseconds))
            result["client_build"] = configuration.recorder.clientBuild
            result["recorder_build"] = configuration.recorder.expectedPeerBuild
            result["service_build"] = configuration.service.expectedPeerBuild
            result["recorder_generation"] = .string(configuration.recorder.processGeneration)
            result["service_generation"] = .string(configuration.service.processGeneration)
            await session.start(configuration: configuration)
            let useful = DispatchTime.now().uptimeNanoseconds
            let state = session.model.state
            // The configuration names no catalog: report the one the service pinned and delivered.
            result["catalog_sha256"] = session.model.catalogSHA256.map(RecordingJSON.string) ?? .null
            result["catalog_reference"] = session.model.catalogReference ?? .null
            passed = session.model.canRecord && state.prompt != nil && !state.showsStop
                && session.model.catalogSHA256 != nil
            result["service_ready"] = .boolean(state.serviceReady)
            result["context_ready"] = .boolean(state.contextReady)
            result["native_device_ready"] = .boolean(state.deviceReady)
            result["storage_ready"] = .boolean(state.storageReady)
            result["record_enabled"] = .boolean(session.model.canRecord)
            result["status"] = .string(String(state.status.prefix(512)))
            result["phase"] = .string(state.phase.rawValue)
            result["selection"] = state.selection
            result["group_id"] = state.groupID.map(RecordingJSON.string) ?? .null
            result["prompt_id"] = state.prompt.map { .string($0.id) } ?? .null
            result["history_completeness"] = .string(state.historyCompleteness)
            if passed {
                result["useful_connected_uptime_ns"] = .integer(Int64(useful))
                result["entry_to_useful_ms"] = .number(Double(useful - entered) / 1_000_000)
            } else {
                result["error_code"] = state.errorCode.map(RecordingJSON.string) ?? .string("RECORDING.GATE_NOT_READY")
            }
            try await session.model.review.playback.shutdown()
            session.model.close()
            result["channels_closed"] = .boolean(true)
        } catch {
            session.model.close()
            result["error_code"] = .string((error as? RecordingContractError)?.code ?? "RECORDING.INVALID_MESSAGE")
            result["error"] = .string(String(((error as? RecordingContractError)?.message ?? String(describing: error)).prefix(512)))
            passed = false
        }
        result["result"] = .string(passed ? "passed" : "failed")
        result["reported_uptime_ns"] = .integer(Int64(DispatchTime.now().uptimeNanoseconds))
        do {
            try RecordingVerificationReportFile.write(RecordingJSON.object(result).transportData(), path: report)
        } catch {
            FileHandle.standardError.write(Data("Synthetic application verification report could not be published\n".utf8))
            return 74
        }
        return passed ? 0 : 1
    }

    private static func invalid(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: message)
    }
}

/// Fresh private bounded output, never overwriting any existing evidence.
private enum RecordingVerificationReportFile {
    static func validate(_ path: String) throws {
        let parent = try directory(path)
        defer { close(parent) }
        var metadata = stat()
        guard fstatat(parent, URL(fileURLWithPath: path).lastPathComponent, &metadata, AT_SYMLINK_NOFOLLOW) != 0,
              errno == ENOENT else { throw failure() }
    }

    static func write(_ data: Data, path: String) throws {
        guard data.count <= 65_536 else { throw failure() }
        let parent = try directory(path)
        defer { close(parent) }
        let temporary = ".verification-" + UUID().uuidString.lowercased() + ".tmp"
        let descriptor = openat(parent, temporary, O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC | O_WRONLY, 0o600)
        guard descriptor >= 0 else { throw failure() }
        defer { close(descriptor); unlinkat(parent, temporary, 0) }
        try data.withUnsafeBytes { bytes in
            var position = 0
            while position < bytes.count {
                let written = Darwin.write(descriptor, bytes.baseAddress!.advanced(by: position), bytes.count - position)
                if written < 0 && errno == EINTR { continue }
                guard written > 0 else { throw failure() }
                position += written
            }
        }
        guard fsync(descriptor) == 0,
              renameatx_np(parent, temporary, parent, URL(fileURLWithPath: path).lastPathComponent, UInt32(RENAME_EXCL)) == 0,
              fsync(parent) == 0 else { throw failure() }
    }

    private static func directory(_ path: String) throws -> Int32 {
        guard path.utf8.count <= 4096, path.hasPrefix("/"), !path.contains("\0"),
              !path.split(separator: "/").contains(where: { $0 == "." || $0 == ".." }),
              URL(fileURLWithPath: path).path == path else { throw failure() }
        let parentPath = URL(fileURLWithPath: path).deletingLastPathComponent().path
        guard let resolved = realpath(parentPath, nil) else { throw failure() }
        defer { free(resolved) }
        guard String(cString: resolved) == parentPath else { throw failure() }
        var descriptor = open("/", O_RDONLY | O_DIRECTORY | O_CLOEXEC)
        guard descriptor >= 0 else { throw failure() }
        do {
            for component in parentPath.split(separator: "/") {
                let next = openat(descriptor, String(component), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                guard next >= 0 else { throw failure() }
                close(descriptor); descriptor = next
            }
            var metadata = stat()
            guard fstat(descriptor, &metadata) == 0, metadata.st_uid == geteuid(),
                  metadata.st_mode & S_IFMT == S_IFDIR, metadata.st_mode & 0o077 == 0 else { throw failure() }
            return descriptor
        } catch { close(descriptor); throw error }
    }

    private static func failure() -> RecordingContractError {
        RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Verification report requires a fresh bounded file in an owned private resolved directory")
    }
}
