import AppKit
import Combine
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI
import SwiftUI

/// The native `finetune` suite (scripts/native_finetune_fixture.py): the actual app, helper,
/// service and fine-tune worker against a stub F5 runtime. Python re-derives every durable
/// outcome from the fixture root afterwards; this harness proves what the app did and showed.
extension RecordingHarness {
    /// The fixture catalog's prompts (`REFERENCE_PROMPT_ID`, `READING_PROMPT_IDS`).
    static let fineTuneReferencePrompt = "finetune-reference"
    static let fineTuneReadingPrompts = ["finetune-reading-one", "finetune-reading-two"]
    /// Mirrors `SLOW_TRAINING_STEP_SECONDS`: the second job trains for minutes, so a take is
    /// recorded and saved while it trains and it is cancelled mid-training.
    static let slowTrainingStepSeconds = 1.0

    /// Record two takes, fine-tune to a finished run, then start a second job, record and save
    /// a take while it trains, and cancel it. Writes `finetune-report.json`.
    @MainActor static func fineTune(configurationURL: URL, runtimeRoot: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard config.recorder.arguments.contains("--synthetic-pcm") else { throw fineTuneError("Fine-tune harness requires explicit synthetic PCM") }
        let model = RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false },
                                            reviewBackend: AVFoundationRecordingPlaybackBackend(mode: .offline))
        defer { model.close() }
        // Captures, their analysis jobs and a training worker share the host with the gate.
        let observer = ReviewObserver(model, barrierSeconds: 120)
        var lines: [String] = []
        var stages: [String] = []
        let watcher = model.$fineTune.sink { status in
            if let line = status.statusLine, lines.last != line { lines.append(line) }
            if let stage = status.stage?.rawValue, stages.last != stage { stages.append(stage) }
        }
        defer { watcher.cancel() }

        await model.connect()
        try await observer.wait {
            model.canRecord && model.state.historyCompleteness == "complete" && model.fineTune.runtimeConfigured != nil
        }
        guard model.fineTune.runtimeConfigured == true, model.fineTune.phase == .idle, model.fineTuneSlot.enabled,
              model.fineTuneSlot.identifier == "transport.fine-tune" else {
            throw fineTuneError("Fine-tune model is not available with a configured runtime: \(model.fineTuneSlot)")
        }
        var slots = [model.fineTuneSlot.identifier]

        // The fixed workspace must not move because a job starts, runs or ends.
        let host = NSHostingView(rootView: RecordingWorkspace(model: model))
        host.frame = CGRect(x: 0, y: 0, width: 1060, height: 700)
        host.appearance = NSAppearance(named: .aqua); host.layoutSubtreeIfNeeded()
        let baseline = measuredRegions(view: host)
        try validateRegions(baseline, size: host.frame.size, focusMode: false, baseline: nil)
        func requireGeometry() throws {
            host.layoutSubtreeIfNeeded()
            try validateRegions(measuredRegions(view: host), size: host.frame.size, focusMode: false, baseline: baseline)
        }

        var corpus: [String] = []
        for prompt in [fineTuneReferencePrompt, fineTuneReadingPrompts[0]] {
            corpus.append(try await recordAndSave(prompt, model: model, observer: observer).takeID)
        }

        // Job 1 runs to a finished run.
        lines.removeAll(); stages.removeAll()
        model.activateFineTuneSlot()
        guard model.fineTune.isActive, let finishedOperation = model.fineTune.operationID else {
            throw fineTuneError("Fine-tune model did not start a job: \(model.fineTune)")
        }
        slots.append(model.fineTuneSlot.identifier)
        try await observer.wait { model.fineTune.phase.isTerminal }
        try requireGeometry()
        guard model.fineTune.phase == .finished, let finishedRun = model.fineTune.runID else {
            throw fineTuneError("The first fine-tune did not finish: \(model.fineTune.statusLine ?? "no status")")
        }
        guard lines.contains(where: { $0.hasPrefix("Fine-tuning · pass ") }), stages.contains("training") else {
            throw fineTuneError("The first fine-tune showed no training progress: \(lines)")
        }
        let modelReady = "Model ready · " + finishedRun
        try await observer.wait { model.statusRegion.line == modelReady }
        slots.append(model.fineTuneSlot.identifier)
        let finished: RecordingJSON = .object([
            "operation_id": .string(finishedOperation), "run_id": .string(finishedRun),
            "lines": .array(lines.map(RecordingJSON.string)), "stages": .array(stages.map(RecordingJSON.string)),
            "status_region_line": .string(model.statusRegion.line),
        ])

        // Job 2 trains slowly; a take is recorded and saved while it trains; then it is cancelled.
        try RecordingJSON.object(["step_seconds": .number(slowTrainingStepSeconds)]).transportData()
            .write(to: runtimeRoot.appendingPathComponent("stub-mode.json"), options: .atomic)
        lines.removeAll(); stages.removeAll()
        model.activateFineTuneSlot()
        guard model.fineTune.isActive, let cancelledOperation = model.fineTune.operationID,
              cancelledOperation != finishedOperation else {
            throw fineTuneError("Fine-tune model did not start a second job: \(model.fineTune)")
        }
        slots.append(model.fineTuneSlot.identifier)
        try await observer.wait { model.fineTune.stage == .training || model.fineTune.phase.isTerminal }
        guard model.fineTune.phase == .running, let cancelledRun = model.fineTune.runID else {
            throw fineTuneError("The second fine-tune did not reach training: \(model.fineTune.statusLine ?? "no status")")
        }
        let runningBeforeRecord = model.fineTune.phase == .running
        let during = try await recordAndSave(fineTuneReadingPrompts[1], model: model, observer: observer)
        let runningAfterSave = model.fineTune.phase == .running && model.fineTune.operationID == cancelledOperation
        try requireGeometry()
        model.activateFineTuneSlot()
        guard model.fineTune.cancelRequested, model.fineTuneSlot.identifier == "transport.cancel-fine-tune",
              !model.fineTuneSlot.enabled else {
            throw fineTuneError("Cancel fine-tune did not take effect: \(model.fineTuneSlot)")
        }
        try await observer.wait { model.fineTune.phase.isTerminal }
        guard model.fineTune.phase == .cancelled else {
            throw fineTuneError("The second fine-tune did not end cancelled: \(model.fineTune.statusLine ?? "no status")")
        }
        try await observer.wait { model.statusRegion.line == "Fine-tune cancelled" }
        try requireGeometry()
        guard model.fineTuneSlot.enabled else { throw fineTuneError("Fine-tune model is not available after a cancel") }
        slots.append(model.fineTuneSlot.identifier)
        guard lines.contains(where: { $0.hasPrefix("Fine-tuning · pass ") }) else {
            throw fineTuneError("The second fine-tune showed no training progress: \(lines)")
        }
        let cancelled: RecordingJSON = .object([
            "operation_id": .string(cancelledOperation), "run_id": .string(cancelledRun),
            "lines": .array(lines.map(RecordingJSON.string)), "stages": .array(stages.map(RecordingJSON.string)),
            "status_region_line": .string(model.statusRegion.line),
        ])

        let report: RecordingJSON = .object([
            "suite": .string("finetune"), "runtime_configured": .boolean(true),
            "corpus_take_ids": .array(corpus.map(RecordingJSON.string)),
            "during_job_take_id": .string(during.takeID),
            "during_job": .object([
                "running_before_record": .boolean(runningBeforeRecord), "running_after_save": .boolean(runningAfterSave),
                "navigation_available": .boolean(during.navigationAvailable), "save_available": .boolean(during.saveAvailable),
            ]),
            "finished": finished, "cancelled": cancelled,
            "slots": .array(slots.map(RecordingJSON.string)),
            "fixed_geometry": .boolean(true), "microphone_opened": .boolean(false), "window_shown": .boolean(false),
        ])
        try report.transportData().write(to: directory.appendingPathComponent("finetune-report.json"), options: .atomic)
        print(String(decoding: try report.transportData(), as: UTF8.self))
    }

    /// A runtime directory without `bin/python`: Fine-tune model stays in place, disabled, and
    /// says why; pressing it submits nothing. Writes `finetune-unavailable-report.json`.
    @MainActor static func fineTuneUnavailable(configurationURL: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        let model = RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false },
                                            reviewBackend: AVFoundationRecordingPlaybackBackend(mode: .offline))
        defer { model.close() }
        let observer = ReviewObserver(model)
        await model.connect()
        try await observer.wait {
            model.canRecord && model.state.historyCompleteness == "complete"
                && (model.fineTune.runtimeConfigured != nil || model.fineTune.availabilityError != nil)
        }
        let host = NSHostingView(rootView: RecordingWorkspace(model: model))
        host.frame = CGRect(x: 0, y: 0, width: 1060, height: 700)
        host.appearance = NSAppearance(named: .aqua); host.layoutSubtreeIfNeeded()
        try validateRegions(measuredRegions(view: host), size: host.frame.size, focusMode: false, baseline: nil)
        let slot = model.fineTuneSlot
        model.activateFineTuneSlot()
        guard model.fineTune.runtimeConfigured == false, model.fineTune.phase == .idle, model.fineTune.operationID == nil,
              !slot.enabled, slot.identifier == "transport.fine-tune" else {
            throw fineTuneError("A runtime without bin/python left Fine-tune model usable: \(model.fineTune) \(slot)")
        }
        let region = model.statusRegion
        let report: RecordingJSON = .object([
            "suite": .string("finetune-unavailable"), "runtime_configured": .boolean(false),
            "slot": .object(["title": .string(slot.title), "identifier": .string(slot.identifier),
                             "enabled": .boolean(slot.enabled), "help": .string(slot.help)]),
            "status_region_source": .string(region.source.rawValue), "status_region_line": .string(region.line),
            "fixed_geometry": .boolean(true),
        ])
        try report.transportData().write(to: directory.appendingPathComponent("finetune-unavailable-report.json"), options: .atomic)
        print(String(decoding: try report.transportData(), as: UTF8.self))
    }

    /// Select `prompt`, record the synthetic signal, stop and Save without advancing.
    @MainActor private static func recordAndSave(_ prompt: String, model: RecordingWorkspaceModel, observer: ReviewObserver) async throws
        -> (takeID: String, navigationAvailable: Bool, saveAvailable: Bool) {
        let navigationAvailable = model.state.canNavigate
        model.selectPrompt(prompt)
        try await observer.wait { model.state.prompt?.id == prompt && !model.state.navigationPending }
        guard model.canRecord else { throw fineTuneError("Record is unavailable for \(prompt): \(model.recordUnavailableReason)") }
        model.record()
        try await observer.wait { model.waveform.snapshot?.writtenFrames == 4801 }
        model.stop()
        try await observer.wait { model.canSave && model.selectedReview != nil && !model.review.loading }
        guard let takeID = model.state.takeID else { throw fineTuneError("Stopped take is missing its identity") }
        let saveAvailable = model.canSave
        model.save(advance: false)
        try await observer.wait {
            model.state.phase == .ready && !model.state.navigationPending && model.state.takeID == takeID
                && model.state.decision == "kept"
        }
        return (takeID, navigationAvailable, saveAvailable)
    }

    private static func fineTuneError(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.FINETUNE_HARNESS", message: message)
    }
}
