import AppKit
import Darwin
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI

extension RecordingHarness {
    /// `open CONFIG OUTPUT_DIRECTORY` measures the production connection without recording.
    /// Requires explicit synthetic PCM; normal open-context/recovery metadata work still runs.
    /// Writes open-report.json on configuration or connection failure as well as success.
    /// Exit 0 means Record is available; exit 1 means configuration/open/readiness failed.
    /// Invalid CLI arguments or an unwritable/existing report remain harness errors.
    @MainActor
    static func openProbe(configurationURL: URL, directory: URL) async throws -> Int32 {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let reportURL = directory.appendingPathComponent("open-report.json")
        guard !FileManager.default.fileExists(atPath: reportURL.path) else {
            throw RecordingContractError(code: "RECORDING.HARNESS_ARGUMENTS", message: "Open probe requires a new report path")
        }
        let entered = DispatchTime.now().uptimeNanoseconds
        var report: [String: RecordingJSON] = [
            "schema_version": .string("cole-voice-lab.recording-open-probe/v1"),
            "probe_started_uptime_ns": .integer(Int64(entered)),
            "configuration_loaded_uptime_ns": .null,
            "connect_started_uptime_ns": .null,
            "connect_finished_uptime_ns": .null,
            "connect_elapsed_ns": .null,
            "can_record": .boolean(false),
            "history_completeness": .string("unknown"),
            "phase": .string("configuration"),
            "error": .null,
            "record_requested": .boolean(false),
            "microphone_opened": .boolean(false),
            "playback_started": .boolean(false),
            "window_shown": .boolean(false),
            "channels_closed": .boolean(false),
        ]
        var ready = false
        do {
            let configuration = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
            guard configuration.recorder.arguments.contains("--synthetic-pcm") else {
                throw RecordingContractError(code: "RECORDING.HARNESS_ARGUMENTS", message: "Open probe requires explicit synthetic helper PCM")
            }
            report["configuration_loaded_uptime_ns"] = .integer(Int64(DispatchTime.now().uptimeNanoseconds))
            report["client_build"] = configuration.recorder.clientBuild
            report["recorder_build"] = configuration.recorder.expectedPeerBuild
            report["service_build"] = configuration.service.expectedPeerBuild
            report["recorder_generation"] = .string(configuration.recorder.processGeneration)
            report["service_generation"] = .string(configuration.service.processGeneration)
            let model = RecordingWorkspaceModel(configuration: configuration,
                permissionStatus: { .granted }, permissionRequest: { false },
                reviewBackend: AVFoundationRecordingPlaybackBackend(mode: .offline))
            let started = DispatchTime.now().uptimeNanoseconds
            report["connect_started_uptime_ns"] = .integer(Int64(started))
            await model.connect()
            let finished = DispatchTime.now().uptimeNanoseconds
            report["connect_finished_uptime_ns"] = .integer(Int64(finished))
            report["connect_elapsed_ns"] = .integer(Int64(finished - started))
            let state = model.state
            ready = model.canRecord && state.errorCode == nil && model.recoverySupervisorError == nil && model.takeHistoryError == nil
            report["can_record"] = .boolean(model.canRecord)
            report["history_completeness"] = .string(state.historyCompleteness)
            report["history_revision"] = .integer(state.historyRevision)
            // The catalog the service pinned; null when its catalog request was refused.
            report["catalog_sha256"] = model.catalogSHA256.map(RecordingJSON.string) ?? .null
            report["catalog_reference"] = model.catalogReference ?? .null
            report["phase"] = .string(state.phase.rawValue)
            report["status"] = .string(state.status)
            report["state_error_code"] = state.errorCode.map(RecordingJSON.string) ?? .null
            report["service_ready"] = .boolean(state.serviceReady)
            report["context_ready"] = .boolean(state.contextReady)
            report["context_reason"] = .string(state.contextReason)
            report["device_ready"] = .boolean(state.deviceReady)
            report["device_reason"] = .string(state.deviceReason)
            report["storage_ready"] = .boolean(state.storageReady)
            report["storage_reason"] = .string(state.storageReason)
            report["record_unavailable_reason"] = .string(model.recordUnavailableReason)
            report["recovery_supervisor_error"] = model.recoverySupervisorError.map(RecordingJSON.string) ?? .null
            report["take_history_error"] = model.takeHistoryError.map(RecordingJSON.string) ?? .null
            if !ready {
                report["error"] = .object([
                    "code": .string(state.errorCode ?? "RECORDING.GATE_NOT_READY"),
                    "message": .string(model.recoverySupervisorError ?? model.takeHistoryError ?? state.status),
                ])
            }
            model.close()
            report["channels_closed"] = .boolean(true)
        } catch {
            let error = error as? RecordingContractError ?? RecordingContractError(
                code: "RECORDING.INVALID_MESSAGE", message: String(describing: error))
            report["error"] = .object([
                "code": .string(error.code), "message": .string(error.message),
                "context": .object(error.context.mapValues(RecordingJSON.string)),
            ])
        }
        let completed = DispatchTime.now().uptimeNanoseconds
        report["probe_finished_uptime_ns"] = .integer(Int64(completed))
        report["total_elapsed_ns"] = .integer(Int64(completed - entered))
        report["result"] = .string(ready ? "ready" : "failed")
        report["exit_code"] = .integer(ready ? 0 : 1)
        try RecordingJSON.object(report).transportData().write(to: reportURL, options: .withoutOverwriting)
        return ready ? 0 : 1
    }
}
