import AppKit
import CryptoKit
import Darwin
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI

extension RecordingHarness {
    /// One synthetic take while actual history coverage is incomplete. This is diagnostic
    /// evidence, never an acceptance Save & Next or warmed-launch measurement.
    /// Exit: 0 proven, 1 operation/verification failure, 2 incomplete precondition not met,
    /// 124 hard 90-second process deadline (separate deadline-report.json).
    @MainActor
    static func earlyCaptureProbe(configurationURL: URL, directory: URL) async throws -> Int32 {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let reportURL = directory.appendingPathComponent("early-capture-report.json")
        guard !FileManager.default.fileExists(atPath: reportURL.path) else {
            throw earlyCaptureError("Early capture requires a new report path")
        }
        let entered = DispatchTime.now().uptimeNanoseconds
        func descendantIdentities() -> [pid_t: String] {
            let children = RecordingFootprintSampler.childrenByParent()
            var pending = children[getpid()] ?? []
            var identities: [pid_t: String] = [:]
            while let child = pending.popLast() {
                identities[child] = RecordingFootprintSampler.executablePath(child)
                pending.append(contentsOf: children[child] ?? [])
            }
            return identities
        }
        func identityJSON(_ identities: [pid_t: String]) -> RecordingJSON {
            .array(identities.keys.sorted().map { pid in .object([
                "pid": .integer(Int64(pid)), "executable_path": .string(identities[pid] ?? "")]) })
        }
        // A command wrapper may leave an assertion child beneath its exec'd utility.
        // Record that inherited state before the model can launch either owned peer.
        let inheritedIdentities = descendantIdentities()
        let watchdog = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        watchdog.schedule(deadline: .now() + .seconds(90))
        watchdog.setEventHandler {
            let report: RecordingJSON = .object([
                "schema_version": .string("cole-voice-lab.early-capture-deadline/v1"),
                "result": .string("failed"), "exit_code": .integer(124),
                "started_uptime_ns": .integer(Int64(entered)),
                "ended_uptime_ns": .integer(Int64(DispatchTime.now().uptimeNanoseconds)),
                "error": .string("Hard 90-second diagnostic deadline; inspect retained progress output"),
                "channels_closed": .boolean(false), "clean_closure": .boolean(false),
            ])
            if let data = try? report.transportData() {
                try? data.write(to: directory.appendingPathComponent("deadline-report.json"), options: .withoutOverwriting)
            }
            Darwin._exit(124)
        }
        watchdog.resume()
        defer { watchdog.cancel() }
        var report: [String: RecordingJSON] = [
            "schema_version": .string("cole-voice-lab.early-capture-probe/v1"),
            "started_uptime_ns": .integer(Int64(entered)), "expected_frames": .integer(48_000),
            "record_requested": .boolean(false), "stop_requested": .boolean(false),
            "save_requested": .boolean(false), "channels_closed": .boolean(false),
            "clean_closure": .boolean(false), "microphone_opened": .boolean(false),
            "playback_started": .boolean(false), "window_shown": .boolean(false),
            "client_request_deadline_changed": .boolean(false), "error": .null,
            "inherited_descendants_at_entry": identityJSON(inheritedIdentities),
        ]
        var checkpoints: [RecordingJSON] = []
        var actions: [RecordingJSON] = []
        var model: RecordingWorkspaceModel?
        var configuration: RecordingApplicationConfiguration?
        var saveOperationID: String?
        var exitCode: Int32 = 1
        func instant() -> UInt64 { DispatchTime.now().uptimeNanoseconds }
        func checkpoint(_ label: String, _ current: RecordingWorkspaceModel) {
            let value = ReviewObserver.checkpoint(current, label: label)
            checkpoints.append(value)
            if checkpoints.count > 512 { checkpoints.removeFirst(checkpoints.count - 512) }
            if let data = try? value.transportData() { print(String(decoding: data, as: UTF8.self)); fflush(stdout) }
        }
        func action(_ name: String, _ start: UInt64, _ end: UInt64, _ boundary: String) {
            actions.append(.object(["action": .string(name), "started_uptime_ns": .integer(Int64(start)),
                "ended_uptime_ns": .integer(Int64(end)), "duration_ns": .integer(Int64(end - start)),
                "measured_boundary": .string(boundary)]))
        }
        do {
            let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
            configuration = config
            let arguments = config.recorder.arguments
            guard arguments.contains("--synthetic-pcm"),
                  arguments.filter({ $0 == "--synthetic-total-frames" }).count == 1,
                  let position = arguments.firstIndex(of: "--synthetic-total-frames"), position + 1 < arguments.count,
                  arguments[position + 1] == "48000" else {
                throw earlyCaptureError("Early capture requires explicit synthetic PCM and exactly 48000 total frames")
            }
            report["client_build"] = config.recorder.clientBuild
            report["recorder_build"] = config.recorder.expectedPeerBuild
            report["service_build"] = config.service.expectedPeerBuild
            report["asset_root"] = .string(config.assetRoot.path)
            report["catalog_sha256"] = .null
            report["recorder_generation"] = .string(config.recorder.processGeneration)
            report["service_generation"] = .string(config.service.processGeneration)
            let current = RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted },
                permissionRequest: { false }, reviewBackend: AVFoundationRecordingPlaybackBackend(mode: .offline))
            model = current
            current.transitionObserver = { [weak current] _ in
                if let current { checkpoint("state-transition", current) }
            }
            let observer = ReviewObserver(current, barrierSeconds: 15)
            func failure() -> RecordingContractError? {
                if let problem = current.review.failure ?? current.review.playback.failure { return problem }
                if let code = current.state.errorCode { return RecordingContractError(code: code, message: current.state.status) }
                if let message = current.recoverySupervisorError ?? current.takeHistoryError {
                    return earlyCaptureError(message)
                }
                return nil
            }
            func wait(_ predicate: @escaping () -> Bool) async throws {
                // Save failures return to draftReview. The generic observer's error-phase
                // check alone misses those, so terminate on ANY actual named command error.
                try await observer.wait { predicate() || failure() != nil }
                if let error = failure() { throw error }
            }
            let connectStart = instant()
            await current.connect()
            action("connect", connectStart, instant(), "model.connect call to its return; no history-complete wait")
            // The configuration names no catalog; the service delivers the one it pinned.
            report["catalog_sha256"] = current.catalogSHA256.map(RecordingJSON.string) ?? .null
            checkpoint("connected", current)
            if let error = failure() { throw error }
            guard current.canRecord, let promptID = current.state.prompt?.id else {
                throw earlyCaptureError("Connected model does not permit Record")
            }
            guard current.state.historyCompleteness != "complete" else {
                exitCode = 2; throw earlyCaptureError("History completed before Record; incomplete-coverage precondition was not exercised")
            }
            checkpoint("before-record", current)
            let recordStart = instant()
            // No suspension separates the observed incomplete state and actual Record call.
            current.record()
            report["record_requested"] = .boolean(true)
            checkpoint("record-dispatched", current)
            try await wait { (current.waveform.snapshot?.writtenFrames ?? 0) > 0 }
            action("record_to_first_frames", recordStart, instant(), "actual Record call to first accepted written-frame telemetry")
            try await wait { current.waveform.snapshot?.writtenFrames == 48_000 }
            guard current.state.canStop, let takeID = current.state.takeID else {
                throw earlyCaptureError("Synthetic 48000-frame take is not actively stoppable")
            }
            report["take_id"] = .string(takeID)
            checkpoint("before-stop", current)
            let stopStart = instant()
            current.stop()
            report["stop_requested"] = .boolean(true)
            checkpoint("stop-dispatched", current)
            try await wait {
                current.canSave && current.selectedReview?.playback.takeID == takeID &&
                current.selectedReview?.playback.frameCount == 48_000 && current.waveformReady &&
                current.review.canReview && current.review.playback.canPlay && !current.review.loading
            }
            action("stop_to_verified_review", stopStart, instant(), "real Stop call to sealed draft, verified selected source and matching final waveform; independent QC may remain pending")
            checkpoint("review-ready", current)
            guard let selected = current.selectedReview,
                  current.state.prompt?.id == promptID, current.state.takeID == takeID,
                  current.state.sealedFrames == 48_000, current.waveform.snapshot?.writtenFrames == 48_000,
                  selected.retainedRange == 0..<48_000, !current.review.isDirty else {
                throw earlyCaptureError("Stopped review does not bind the exact captured prompt/take/full frame range")
            }
            report["source_sha256"] = .string(selected.playback.sourceSHA256)
            guard !current.saveAdvances, current.state.historyCompleteness != "complete" else {
                exitCode = 2; throw earlyCaptureError("History completed before Save; draft preserved, incomplete plain Save was not exercised")
            }
            report["save_intent_advance"] = .boolean(current.saveAdvances)
            checkpoint("before-plain-save", current)
            let saveStart = instant()
            // The normal no-argument UI action captures its false advance intent here.
            current.save()
            report["save_requested"] = .boolean(true)
            saveOperationID = current.state.activeOperationID
            guard current.state.phase == .saving, saveOperationID != nil else {
                throw earlyCaptureError("Plain Save did not create its actual pending operation")
            }
            checkpoint("plain-save-dispatched", current)
            try await wait { current.state.phase == .ready && current.state.decision == "kept" && !current.state.navigationPending }
            action("plain_save_to_durable_ack", saveStart, instant(), "normal plain Save call to kept take and durable cursor acknowledgment; no Next navigation")
            checkpoint("plain-save-acknowledged", current)
            guard current.state.prompt?.id == promptID, current.state.takeID == takeID,
                  current.state.take.object?["decision_revision"]?.integer == 1,
                  current.state.sealedFrames == 48_000 else {
                throw earlyCaptureError("Plain Save changed prompt/take or failed to persist its first kept decision")
            }
            guard let operationID = saveOperationID else { throw earlyCaptureError("Save operation identity was lost") }
            let operationPath = config.assetRoot.appendingPathComponent("provenance/recording-takes/.operations/\(operationID).json")
            let operationBytes = try RecordingPrivateFile.read(operationPath.path, maximumBytes: 8_388_608)
            let operation = try RecordingJSON.parse(operationBytes)
            guard operation.object?["operation_id"] == .string(operationID), operation.object?["kind"] == .string("save"),
                  operation.object?["save"]?.object?["advance"] == .boolean(false),
                  operation.object?["save"]?.object?["navigation"] == nil || operation.object?["save"]?.object?["navigation"] == .null,
                  operation.object?["take"]?.object?["reservation"]?.object?["take_id"] == .string(takeID),
                  operation.object?["take"]?.object?["decision"] == .string("kept"),
                  operation.object?["take"]?.object?["seal"]?.object?["audio"]?.object?["frames"] == .integer(48_000),
                  operation.object?["cursor"]?.object?["selection"]?.object?["prompt_id"] == .string(promptID),
                  operation.object?["cursor"]?.object?["selection"]?.object?["take_id"] == .string(takeID) else {
                throw earlyCaptureError("Immutable Save journal does not prove exact nonadvancing kept take")
            }
            let originalPath = selected.playback.assetRoot.appendingPathComponent(selected.playback.relativePath)
            let original = try RecordingPrivateFile.read(originalPath.path, maximumBytes: 1_048_576)
            let originalSHA = SHA256.hash(data: original).map { String(format: "%02x", $0) }.joined()
            guard originalSHA == selected.playback.sourceSHA256,
                  operation.object?["take"]?.object?["seal"]?.object?["original_sha256"] == .string(originalSHA),
                  operation.object?["take"]?.object?["seal"]?.object?["original_size_bytes"] == .integer(Int64(original.count)) else {
                throw earlyCaptureError("Saved immutable operation and verified original differ")
            }
            report["save_operation_path"] = .string(operationPath.path)
            report["save_operation_sha256"] = .string(SHA256.hash(data: operationBytes).map { String(format: "%02x", $0) }.joined())
            report["original_path"] = .string(originalPath.path)
            report["original_size_bytes"] = .integer(Int64(original.count))
            report["durable_take_frame_decision_verified"] = .boolean(true)
            exitCode = 0
        } catch {
            let failure = error as? RecordingContractError ?? earlyCaptureError(String(describing: error))
            report["error"] = .object(["code": .string(failure.code), "message": .string(failure.message),
                "context": .object(failure.context.mapValues(RecordingJSON.string))])
            if let model { checkpoint("failure", model) }
        }
        if let model {
            report["transitions"] = .array(model.transitions.map { transition in .object([
                "uptime_ns": .integer(Int64(transition.monotonicNanoseconds)), "phase": .string(transition.phase),
                "prompt_id": transition.promptID.map(RecordingJSON.string) ?? .null,
                "take_id": transition.takeID.map(RecordingJSON.string) ?? .null,
                "operation_id": transition.operationID.map(RecordingJSON.string) ?? .null,
                "status": .string(transition.status)]) })
            model.transitionObserver = nil
            let currentIdentities = descendantIdentities()
            let excluded = currentIdentities.filter { pid, path in
                !path.isEmpty && inheritedIdentities[pid] == path
            }
            let ownedPaths = currentIdentities.filter { excluded[$0.key] == nil }
            let owned = ownedPaths.keys.sorted()
            report["excluded_inherited_descendants_at_close"] = identityJSON(excluded)
            report["owned_descendants_at_close"] = identityJSON(ownedPaths)
            report["owned_child_pids_at_close"] = .array(owned.map { .integer(Int64($0)) })
            // Stop an unfinished synthetic capture on any failure; never auto-Save a draft.
            if model.state.canStop { model.stop() }
            do { try await model.review.playback.shutdown() }
            catch { report["closure_error"] = .string(String(describing: error)); if exitCode == 0 { exitCode = 1 } }
            model.close()
            report["channels_closed"] = .boolean(true)
            let closeDeadline = instant() + 8_000_000_000
            func survivingChildren() -> [pid_t] {
                owned.filter { pid in
                    let path = RecordingFootprintSampler.executablePath(pid)
                    return !path.isEmpty && path == ownedPaths[pid]
                }
            }
            while !survivingChildren().isEmpty && instant() < closeDeadline {
                try await Task.sleep(for: .milliseconds(50))
            }
            let surviving = survivingChildren()
            report["surviving_owned_child_pids"] = .array(surviving.map { .integer(Int64($0)) })
            if !surviving.isEmpty {
                let problem: RecordingJSON = .object([
                    "code": .string("RECORDING.EARLY_CAPTURE_CLOSURE_FAILED"),
                    "message": .string("Owned child processes remained after channel closure"),
                    "context": .object(["surviving_owned_child_pids": .string(surviving.map(String.init).joined(separator: ","))]),
                ])
                report["closure_error"] = problem
                if report["error"] == .null { report["error"] = problem }
            }
            report["clean_closure"] = .boolean(surviving.isEmpty && report["closure_error"] == nil)
            if report["clean_closure"] == .boolean(false) && exitCode == 0 { exitCode = 1 }
        }
        report["save_operation_id"] = saveOperationID.map(RecordingJSON.string) ?? .null
        if let config = configuration, let operationID = saveOperationID {
            let path = config.assetRoot.appendingPathComponent("provenance/recording-takes/.operations/\(operationID).json")
            report["save_operation_exists_after_failure_or_success"] = .boolean(FileManager.default.fileExists(atPath: path.path))
            report["save_operation_candidate_path"] = .string(path.path)
        }
        report["checkpoints"] = .array(checkpoints)
        report["actions"] = .array(actions)
        report["ended_uptime_ns"] = .integer(Int64(instant()))
        report["total_duration_ns"] = .integer(Int64(instant() - entered))
        report["result"] = .string(exitCode == 0 ? "proven" : exitCode == 2 ? "inconclusive" : "failed")
        report["exit_code"] = .integer(Int64(exitCode))
        try RecordingJSON.object(report).transportData().write(to: reportURL, options: .withoutOverwriting)
        return exitCode
    }

    private static func earlyCaptureError(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.EARLY_CAPTURE_PROBE", message: message)
    }
}
