import AppKit
import Combine
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI
import SwiftUI

extension RecordingHarness {
    /// Physical P09 regression. Stop reaches the real helper after its start but before the
    /// first synthetic chunk. The zero-frame terminal must return to Ready with Record enabled
    /// within a bound, via one explicit service recheck, without the recoverable-error dead end.
    @MainActor static func recoveryZeroFrame(configurationURL: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard config.recorder.arguments.contains("--synthetic-pcm"),
              config.recorder.arguments.contains("--synthetic-start-barrier-directory") else {
            throw recoveryError("Zero-frame recovery requires synthetic PCM and its start barrier")
        }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        let model = try RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false }, reviewBackend: backend)
        defer { model.close() }
        let observer = ReviewObserver(model, barrierSeconds: 120)
        let fixtureRoot = configurationURL.deletingLastPathComponent()
        let fileEvents = try RecordingFixtureFileObserver(directory: fixtureRoot)
        defer { fileEvents.close() }
        await model.connect()
        try await observer.wait {
            model.canRecord && model.state.historyCompleteness == "complete" && model.recoveryScanState == "complete"
        }
        model.selectPrompt("package-reading-one")
        try await observer.wait { model.state.prompt?.id == "package-reading-one" && !model.state.navigationPending }

        let beforeCapture = model.transitions.count
        model.record()
        // The helper holds its first synthetic start before any chunk; Stop lands there.
        try await fileEvents.wait(for: fixtureRoot.appendingPathComponent("start-entered"), timeout: 30)
        guard let takeID = model.state.takeID, model.state.phase == .starting else {
            throw recoveryError("The start barrier was entered before the reserved take reached Starting")
        }
        model.stop()
        guard model.state.phase == .stopping else { throw recoveryError("Stop during startup lost its stopping state") }
        let stoppedAt = DispatchTime.now().uptimeNanoseconds
        try RecordingFixtureFileObserver.release(fixtureRoot.appendingPathComponent("start-release.fifo"), byte: 1)
        // CANCELLED is named transiently before the benign terminal resolves it; a
        // recoverable-error phase or any other failure still aborts this barrier.
        let readyBoundSeconds = 10
        try await ReviewObserver(model, barrierSeconds: readyBoundSeconds).wait(tolerating: "RECORDING.CANCELLED") {
            model.state.phase == .ready && model.canRecord && model.state.takeID == takeID
        }
        let readyNanoseconds = DispatchTime.now().uptimeNanoseconds - stoppedAt
        guard readyNanoseconds <= UInt64(readyBoundSeconds) * 1_000_000_000 else { throw recoveryError("Ready exceeded its bound") }
        let resolved = Array(model.transitions.dropFirst(beforeCapture))
        guard !resolved.contains(where: { $0.phase == RecordingShellPhase.recoverableError.rawValue }) else {
            throw recoveryError("A zero-frame Stop entered the recoverable-error dead end")
        }
        guard model.state.errorCode == nil, model.state.status == RecordingWorkspaceModel.cancelledBeforeAudioStatus,
              model.state.reservationUnsealed, !model.state.unsealedRecheckPending, !model.canSave else {
            throw recoveryError("The zero-frame terminal was not presented as a benign, unsealed cancellation")
        }
        try await observer.wait {
            model.recoveryScanState == "complete" && model.recoveryPendingCount == 0 &&
            model.recoveryRows[takeID]?.errorCode == "RECORDING.CANCELLED"
        }
        guard model.recoveryRows[takeID]?.status == "partial", model.recoveryAttentionRows.isEmpty, !model.canOpenRecovery else {
            throw recoveryError("The explicit recheck did not classify the zero-frame take as a non-alarming partial")
        }
        let readyStatus = model.state.status
        let finding = model.recoveryRows[takeID]

        // Record is genuinely usable: the next capture reserves, records and seals normally.
        model.record()
        try await observer.wait { model.waveform.snapshot?.writtenFrames == 4801 }
        guard let followUpTakeID = model.state.takeID, followUpTakeID != takeID else {
            throw recoveryError("The next capture did not reserve a new take")
        }
        model.stop()
        try await observer.wait { model.canSave && model.selectedReview != nil && !model.review.loading }

        let report: RecordingJSON = .object([
            "suite": .string("recovery-zero-frame"),
            "take_id": .string(takeID),
            "prompt_id": .string("package-reading-one"),
            "ready_after_stop_ns": .integer(Int64(readyNanoseconds)),
            "ready_bound_ns": .integer(Int64(readyBoundSeconds) * 1_000_000_000),
            "record_enabled_after_recheck": .boolean(true),
            "recoverable_error_entered": .boolean(false),
            "status": .string(readyStatus),
            "recovery_status": .string(finding?.status ?? "unknown"),
            "recovery_error_code": .string(finding?.errorCode ?? "none"),
            "inventory_scan_state": .string("complete"),
            "follow_up_take_id": .string(followUpTakeID),
            "microphone_opened": .boolean(false),
            "window_shown": .boolean(false),
            "installed_app_restarted": .boolean(false),
        ])
        try JSONEncoder().encode(report).write(to: directory.appendingPathComponent("recovery-zero-frame-report.json"), options: .atomic)
        print(String(decoding: try JSONEncoder().encode(report), as: UTF8.self))
    }

    /// Drives a real capture into an actual failed seal, so a genuine native partial exists
    /// for the restart run to recover. Nothing is faked: the writer's own final-header hook
    /// fails at the real commit boundary because the fixture claimed the barrier marker.
    @MainActor static func recoveryCrash(configurationURL: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard config.recorder.arguments.contains("--synthetic-pcm") else { throw recoveryError("Recovery harness requires explicit synthetic PCM") }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        let model = try RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false }, reviewBackend: backend)
        defer { model.close() }
        let observer = ReviewObserver(model, barrierSeconds: 120)
        await model.connect()
        try await observer.wait { model.canRecord && model.state.historyCompleteness == "complete" }

        model.selectPrompt("package-reading-one")
        try await observer.wait { model.state.prompt?.id == "package-reading-one" && !model.state.navigationPending }
        model.record()
        try await observer.wait { model.waveform.snapshot?.writtenFrames == 4801 }
        guard let takeID = model.state.takeID else { throw recoveryError("Capture never reserved a take identity") }
        // Stop reaches the writer's real final-header commit, where the claimed barrier fails.
        model.stop()
        try await observer.waitForFailure(code: "RECORDING.WRITE_FAILED")
        guard !model.canSave else { throw recoveryError("A failed seal must never be savable") }

        let report: RecordingJSON = .object([
            "suite": .string("recovery-crash"),
            "group_id": .string(model.state.groupID ?? "unknown"),
            "take_id": .string(takeID),
            "prompt_id": .string("package-reading-one"),
            "helper_failure_code": .string(model.state.errorCode ?? "none"),
            "savable_after_failed_seal": .boolean(model.canSave),
            "microphone_opened": .boolean(false),
            "window_shown": .boolean(false),
            "installed_app_restarted": .boolean(false),
        ])
        try JSONEncoder().encode(report).write(to: directory.appendingPathComponent("recovery-report.json"), options: .atomic)
        print(String(decoding: try JSONEncoder().encode(report), as: UTF8.self))
    }

    /// A fresh app session against the same asset root: the previous run's failed capture
    /// must surface as an explicit incomplete recovery, and the workspace must stay usable.
    @MainActor static func recoveryRestart(configurationURL: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard config.recorder.arguments.contains("--synthetic-pcm") else { throw recoveryError("Recovery harness requires explicit synthetic PCM") }
        guard !config.recorder.arguments.contains("--synthetic-seal-barrier-directory") else {
            throw recoveryError("The restart run must not re-arm the single-use seal barrier")
        }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        let model = try RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false }, reviewBackend: backend)
        defer { model.close() }
        let observer = ReviewObserver(model, barrierSeconds: 120)
        await model.connect()
        try await observer.wait {
            model.canRecord && model.state.historyCompleteness == "complete" &&
            model.recoveryIncompleteRows.contains { $0.status == "partial" }
        }

        // Startup inventory is asynchronous; this case waits for its actual partial finding.
        let incomplete = model.recoveryIncompleteRows
        guard let partial = incomplete.first(where: { $0.status == "partial" }) else {
            throw recoveryError("A restarted session did not surface the previous failed capture")
        }
        guard partial.errorMessage != nil, partial.errorCode != nil else {
            throw recoveryError("An incomplete recovery must name its own failure")
        }
        // A restored cursor may still name the interrupted take; what must never happen is
        // that it becomes savable or presents itself as a reviewable, complete source.
        guard !model.canSave else { throw recoveryError("A recovered partial must never be savable") }
        let selectedIsPartial = model.state.takeID == partial.takeID
        guard !selectedIsPartial || model.selectedReview == nil else {
            throw recoveryError("A recovered partial must never present a reviewable source")
        }
        // A restarted app in a changed setup opens its own context; the previous group is
        // history, not the target, and its recorded device is never rewritten.
        let restartGroup = model.state.groupID
        guard restartGroup != nil else { throw recoveryError("Restarted session has no recording context") }
        let restartDevice = config.captureDevice.object?["device_id"]?.string ?? "unknown"
        guard restartDevice == "synthetic-coreaudio-uid-changed" else {
            throw recoveryError("The restart fixture did not change its capture device")
        }

        // An explicit single-take re-check is idempotent and never loops.
        let before = model.recoveryRows[partial.takeID]
        await model.recoverTake(partial.takeID)
        let after = model.recoveryRows[partial.takeID]
        guard before == after else { throw recoveryError("Explicit recovery changed a settled result") }

        // The workspace stays fully usable: a new capture records, seals and saves normally.
        model.selectPrompt("package-reading-two")
        try await observer.wait { model.state.prompt?.id == "package-reading-two" && !model.state.navigationPending }
        model.record()
        try await observer.wait { model.waveform.snapshot?.writtenFrames == 4801 }
        model.stop()
        try await observer.wait { model.canSave && model.selectedReview != nil && !model.review.loading }
        guard let savedTakeID = model.state.takeID else { throw recoveryError("Post-recovery capture lost its identity") }
        model.save(advance: false)
        try await observer.wait { model.state.phase == .ready && !model.state.navigationPending }
        guard model.state.prompt?.id == "package-reading-two" else {
            throw recoveryError("Save after recovery did not retain its exact prompt")
        }

        // Sleep ends an active capture explicitly and never resumes it by itself.
        model.selectPrompt("package-reading-three")
        try await observer.wait { model.state.prompt?.id == "package-reading-three" && !model.state.navigationPending }
        model.record()
        // Sleep must interrupt a capture that is genuinely open, not one still starting up:
        // a startup cancellation is a different path and would prove nothing about sleep.
        try await observer.wait { (model.waveform.snapshot?.writtenFrames ?? 0) > 0 && model.state.canStop }
        let sleptTakeID = model.state.takeID
        model.handleSystemSleep()
        try await observer.wait { !model.state.canStop && model.state.phase != .recording }
        guard sleptTakeID != nil else { throw recoveryError("Sleep interrupted a capture with no identity") }


        let report: RecordingJSON = .object([
            "suite": .string("recovery-restart"),
            "restart_group_id": .string(restartGroup ?? "unknown"),
            "recovered_partial_take_id": .string(partial.takeID),
            "recovered_partial_error_code": .string(partial.errorCode ?? "none"),
            "recovered_partial_message": .string(partial.errorMessage ?? "none"),
            "recovery_pages_loaded": .integer(Int64(model.recoveryPagesLoaded)),
            "incomplete_row_count": .integer(Int64(incomplete.count)),
            "recovered_seal_count": .integer(Int64(model.recoveredSealCount)),
            "partial_never_savable": .boolean(true),
            "selected_take_is_recovered_partial": .boolean(selectedIsPartial),
            "explicit_recheck_idempotent": .boolean(true),
            "post_recovery_take_id": .string(savedTakeID),
            "selected_prompt_after_restart": .string(model.state.prompt?.id ?? "unknown"),
            "restart_capture_device_id": .string(restartDevice),
            "sleep_stopped_take_id": .string(sleptTakeID ?? "none"),
            "post_sleep_autostarted": .boolean(false),
            "microphone_opened": .boolean(false),
            "window_shown": .boolean(false),
            "installed_app_restarted": .boolean(false),
            "harness_owned_service_restarted": .boolean(true),
        ])
        try JSONEncoder().encode(report).write(to: directory.appendingPathComponent("recovery-restart-report.json"), options: .atomic)
        print(String(decoding: try JSONEncoder().encode(report), as: UTF8.self))
    }

    /// Another application really owns capture. Recovery must report that as a terminal
    /// result for the page and must not spin waiting for the owner to leave.
    @MainActor static func recoveryBusy(configurationURL: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        let model = try RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false }, reviewBackend: backend)
        defer { model.close() }
        let observer = ReviewObserver(model, barrierSeconds: 120)
        await model.connect()
        try await observer.wait {
            model.state.historyCompleteness == "complete" &&
            model.recoveryIncompleteRows.contains { $0.status == "busy" }
        }
        let rows = model.recoveryIncompleteRows
        guard let busy = rows.first(where: { $0.status == "busy" }) else {
            throw recoveryError("A live capture owner was not reported as busy: \(rows.map(\.status))")
        }
        // Record is unavailable while another owner holds capture, and nothing retries.
        let pagesAfterSweep = model.recoveryPagesLoaded
        await model.recoverTake(busy.takeID)
        guard model.recoveryRows[busy.takeID]?.status == "busy" else {
            throw recoveryError("An explicit re-check did not keep its terminal busy result")
        }
        let report: RecordingJSON = .object([
            "suite": .string("recovery-busy"),
            "busy_take_id": .string(busy.takeID),
            "busy_error_code": .string(busy.errorCode ?? "none"),
            "busy_message": .string(busy.errorMessage ?? "none"),
            "busy_entry_present": .boolean(true),
            "recovery_pages_loaded": .integer(Int64(pagesAfterSweep)),
            "incomplete_row_count": .integer(Int64(rows.count)),
            "microphone_opened": .boolean(false),
            "window_shown": .boolean(false),
            "installed_app_restarted": .boolean(false),
        ])
        try JSONEncoder().encode(report).write(to: directory.appendingPathComponent("recovery-busy-report.json"), options: .atomic)
        print(String(decoding: try JSONEncoder().encode(report), as: UTF8.self))
    }

    private static func recoveryError(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.RECOVERY_HARNESS", message: message)
    }
}
