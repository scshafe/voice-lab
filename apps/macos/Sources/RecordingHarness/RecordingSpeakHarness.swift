import AppKit
import Combine
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI
import SwiftUI

/// The native `speak` suite (scripts/native_speak_fixture.py): the actual app, helper, service
/// and Speak worker against a stub F5 runtime and two finished runs. Python re-reads every
/// clip, record and job afterwards; this harness proves what the app did and showed.
extension RecordingHarness {
    /// Mirrors scripts/native_speak_fixture.py.
    static let speakLatestRun = "finetune-speak-latest"
    static let speakEarlierRun = "finetune-speak-earlier"
    static let speakFirstReference = "finetune-reference"
    static let speakSecondReference = "finetune-reference-two"
    static let speakRecordedPrompt = "finetune-reading-one"
    static let speakSlowSynthesisSeconds = 60.0
    static let speakTrainingStepSeconds = 0.05
    static let speakSlowTrainingStepSeconds = 1.0

    /// Everything the suite asks of the Speak page, in order; writes `speak-report.json`.
    @MainActor static func speak(configurationURL: URL, runtimeRoot: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard config.recorder.arguments.contains("--synthetic-pcm") else { throw speakError("Speak harness requires explicit synthetic PCM") }
        // The harness holds the offline speech backend so it can render what Speak plays.
        let speechBackend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        let model = RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false },
                                            reviewBackend: AVFoundationRecordingPlaybackBackend(mode: .offline),
                                            speechBackend: speechBackend)
        defer { model.close() }
        let speak = model.speak
        // Captures, analysis and workers share the host; a deadline case waits its seconds.
        let observer = ReviewObserver(model, barrierSeconds: 120)
        var phases: [String: [String]] = [:]
        var announcements: [String] = []
        let requestWatcher = speak.$request.sink { request in
            guard let request else { return }
            var seen = phases[request.operationID, default: []]
            if seen.last != request.phase.rawValue { seen.append(request.phase.rawValue) }
            phases[request.operationID] = seen
        }
        let announcementWatcher = speak.$announcement.sink { if let text = $0?.text { announcements.append(text) } }
        defer { requestWatcher.cancel(); announcementWatcher.cancel() }
        func setStubMode(_ mode: [String: RecordingJSON]) throws {
            try RecordingJSON.object(mode).transportData()
                .write(to: runtimeRoot.appendingPathComponent("stub-mode.json"), options: .atomic)
        }

        await model.connect()
        try await observer.wait {
            model.canRecord && model.state.historyCompleteness == "complete" && speak.runsKnown
                && model.fineTune.runtimeConfigured != nil
        }

        // The model and reference choices come from the service's finished runs.
        let choices = speak.modelChoices
        guard choices.map(\.id) == ["latest", speakEarlierRun, "base"],
              choices.map(\.title) == ["Latest · " + speakLatestRun, speakEarlierRun, "Base"],
              speak.referenceChoices.map(\.id) == [speakFirstReference, speakSecondReference],
              speak.selectedReferencePromptID == speakFirstReference else {
            throw speakError("The finished runs are not offered as expected: \(choices) \(speak.referenceChoices)")
        }
        let emptyReason = speak.unavailableReason
        guard emptyReason == RecordingSpeakModel.emptyTextReason, model.fineTuneSlot.enabled else {
            throw speakError("Speak or Fine-tune model is unavailable at the start: \(emptyReason ?? "-") \(model.fineTuneSlot)")
        }

        // The window's page container: Record's regions, then the Speak page, which has none.
        let host = NSHostingView(rootView: RecordingPageContainer(model: model))
        host.frame = CGRect(x: 0, y: 0, width: 1060, height: 700)
        host.appearance = NSAppearance(named: .aqua)
        try await settlePage(.record, model: model, host: host)
        let baseline = measuredRegions(view: host)
        try validateRegions(baseline, size: host.frame.size, focusMode: false, baseline: nil)
        model.showPage(.speak)
        try await settlePage(.speak, model: model, host: host)
        guard measuredRegions(view: host).isEmpty else { throw speakError("The Speak page carries Record region probes") }

        // 1. Latest, with the default reference.
        let latest = try await speakText("The latest model reads this sentence.", speak: speak, observer: observer)
        guard latest.phase == .finished, let latestClip = speak.clips.first(where: { $0.id == latest.operationID }),
              latestClip.runID == speakLatestRun, latestClip.referenceRunID == speakLatestRun,
              latestClip.referencePromptID == speakFirstReference, latestClip.modelTitle == "Latest · " + speakLatestRun else {
            throw speakError("Latest did not speak with its run and first reference: \(latest) \(speak.clips)")
        }
        // Its clip loads as a speech source and plays through the offline engine.
        try await observer.wait { speak.canPlay || speak.clipFailure != nil }
        guard speak.clipFailure == nil, let loaded = speak.playback.source, loaded.takeID == latestClip.id,
              loaded.format == .speech, loaded.frameCount == latestClip.frames else {
            throw speakError("The finished clip did not load as speech: \(speak.clipFailure ?? "-") \(String(describing: speak.playback.source))")
        }
        speak.playPause()
        try await observer.wait { speak.playback.phase == .playing || speak.playback.failure != nil || speak.playbackNotice != nil }
        guard speak.playback.phase == .playing else { throw speakError("The clip did not play: \(speak.playbackNotice ?? speak.playback.failure?.message ?? "-")") }
        // Half the clip: rendering all of it would end the segment and stop the transport.
        let half = Int(latestClip.frames / 2)
        var rendered = 0
        var peak: Float = 0
        while rendered < half {
            let samples = try await speechBackend.renderOffline(frames: UInt32(min(4096, half - rendered)))
            rendered += samples.count
            peak = max(peak, samples.map(abs).max() ?? 0)
        }
        guard speak.playback.phase == .playing else { throw speakError("The clip stopped while half of it was rendered") }
        speak.playPause()
        try await observer.wait { speak.playback.phase == .paused }
        let played: RecordingJSON = .object([
            "operation_id": .string(latestClip.id), "format": .string(loaded.format.rawValue),
            "source_frames": .integer(loaded.frameCount), "rendered_frames": .integer(Int64(rendered)),
            "rendered_peak": .number(Double(peak)), "phase_after_pause": .string(speak.playback.phase.rawValue),
        ])

        // 2. Base, with the second reference named.
        speak.selectedModelID = "base"
        guard speak.referenceChoices.map(\.id) == [speakFirstReference, speakSecondReference] else {
            throw speakError("Base does not offer the latest run's references: \(speak.referenceChoices)")
        }
        speak.selectedReferencePromptID = speakSecondReference
        let base = try await speakText("Base reads a different sentence, so its clip differs.", speak: speak, observer: observer)
        guard base.phase == .finished, let baseClip = speak.clips.first(where: { $0.id == base.operationID }),
              baseClip.modelID == "base", baseClip.runID == nil, baseClip.referenceRunID == speakLatestRun,
              baseClip.referencePromptID == speakSecondReference, baseClip.modelTitle == "Base" else {
            throw speakError("Base did not speak with the latest run's named reference: \(base) \(speak.clips)")
        }

        // 3. The earlier run, with its own (only) reference.
        speak.selectedModelID = speakEarlierRun
        guard speak.referenceChoices.map(\.id) == [speakSecondReference], speak.selectedReferencePromptID == speakSecondReference else {
            throw speakError("The earlier run does not offer its own reference: \(speak.referenceChoices)")
        }
        let earlier = try await speakText("The earlier model reads this sentence.", speak: speak, observer: observer)
        guard earlier.phase == .finished, let earlierClip = speak.clips.first(where: { $0.id == earlier.operationID }),
              earlierClip.runID == speakEarlierRun, earlierClip.referenceRunID == speakEarlierRun,
              earlierClip.referencePromptID == speakSecondReference else {
            throw speakError("The earlier run did not speak as itself: \(earlier) \(speak.clips)")
        }
        speak.selectedModelID = "latest"

        // 4. A slow request is cancelled while it runs.
        try setStubMode(["step_seconds": .number(speakTrainingStepSeconds), "synthesis_seconds": .number(speakSlowSynthesisSeconds)])
        let cancelled = try await startText("This request is cancelled.", speak: speak, observer: observer)
        guard speak.canCancel else { throw speakError("A running request cannot be cancelled: \(speak.cancelUnavailableReason ?? "-")") }
        speak.cancel()
        let cancellingLine = speak.statusLine
        try await observer.wait { speak.request.map { !$0.phase.isActive } ?? false }
        guard speak.request?.operationID == cancelled, speak.request?.phase == .cancelled,
              speak.statusLine == "Speak cancelled", speak.canSpeak else {
            throw speakError("The slow request did not end cancelled: \(String(describing: speak.request))")
        }

        // 5. A request past the (test-only, shortened) deadline fails with the service's words.
        let late = try await startText("This request runs past its deadline.", speak: speak, observer: observer)
        try await observer.wait { speak.request.map { !$0.phase.isActive } ?? false }
        guard let failed = speak.request, failed.operationID == late, failed.phase == .failed,
              failed.errorCode == "FINETUNE.SYNTHESIS_DEADLINE", let deadlineMessage = failed.message,
              speak.statusLine == deadlineMessage, speak.needsAttention else {
            throw speakError("The late request did not fail by its deadline: \(String(describing: speak.request))")
        }

        // 6. While Speak generates, Fine-tune model waits for it and sends nothing.
        let generating = try await startText("This request holds the GPU.", speak: speak, observer: observer)
        let heldSlot = model.fineTuneSlot
        let jobBefore = model.fineTune.operationID
        model.activateFineTuneSlot()
        guard !heldSlot.enabled, heldSlot.help.hasPrefix(RecordingWorkspaceModel.fineTuneWaitForSpeakReason),
              heldSlot.identifier == "transport.fine-tune", model.fineTune.operationID == jobBefore, !model.fineTune.isActive else {
            throw speakError("Fine-tune model did not wait for Speak: \(heldSlot) \(model.fineTune)")
        }
        speak.cancel()
        try await observer.wait { speak.request.map { !$0.phase.isActive } ?? false }
        guard speak.request?.operationID == generating, speak.request?.phase == .cancelled else {
            throw speakError("The GPU-holding request did not end cancelled: \(String(describing: speak.request))")
        }
        try setStubMode(["step_seconds": .number(speakTrainingStepSeconds)])

        // 7. The capture interlock: Speak playback stops for a take and cannot start or speak
        // until the take is saved; the page cannot change meanwhile.
        speak.selectClip(latestClip.id)
        try await observer.wait { speak.canPlay }
        speak.playPause()
        try await observer.wait { speak.playback.phase == .playing }
        model.selectPrompt(speakRecordedPrompt)
        try await observer.wait { model.state.prompt?.id == speakRecordedPrompt && !model.state.navigationPending }
        guard model.canRecord else { throw speakError("Record is unavailable: \(model.recordUnavailableReason)") }
        model.record()
        try await observer.wait { model.waveform.snapshot?.writtenFrames == 4801 }
        let duringCapture: RecordingJSON = .object([
            "phase": .string(model.state.phase.rawValue),
            "speak_playback_phase": .string(speak.playback.phase.rawValue),
            "speak_capture_owned": .boolean(speak.playback.captureOwned),
            "can_play": .boolean(speak.canPlay), "play_reason": speak.playUnavailableReason.map(RecordingJSON.string) ?? .null,
            "can_speak": .boolean(speak.canSpeak), "speak_reason": speak.unavailableReason.map(RecordingJSON.string) ?? .null,
            "can_switch_page": .boolean(model.canSwitchPage),
            "page_switch_reason": model.pageSwitchUnavailableReason.map(RecordingJSON.string) ?? .null,
        ])
        let requestsBefore = speak.request?.operationID
        speak.playPause()
        speak.speak()
        model.showPage(.record)
        guard model.state.showsStop, speak.playback.captureOwned, speak.playback.phase != .playing, !speak.canPlay,
              speak.playUnavailableReason == RecordingSpeakModel.playbackDuringCaptureReason,
              speak.unavailableReason == RecordingSpeakModel.recordingReason, speak.request?.operationID == requestsBefore,
              !model.canSwitchPage, model.page == .speak else {
            throw speakError("Speak did not yield to the take: \(duringCapture)")
        }
        model.stop()
        try await observer.wait { model.canSave && model.selectedReview != nil && !model.review.loading }
        guard let takeID = model.state.takeID else { throw speakError("The stopped take has no identity") }
        let afterStop: RecordingJSON = .object([
            "speak_capture_owned": .boolean(speak.playback.captureOwned), "can_speak": .boolean(speak.canSpeak),
        ])
        model.save(advance: false)
        let whileSaving: RecordingJSON = .object([
            "phase": .string(model.state.phase.rawValue),
            "speak_reason": speak.unavailableReason.map(RecordingJSON.string) ?? .null,
            "page_switch_reason": model.pageSwitchUnavailableReason.map(RecordingJSON.string) ?? .null,
        ])
        try await observer.wait {
            model.state.phase == .ready && !model.state.navigationPending && model.state.takeID == takeID && model.state.decision == "kept"
        }
        try await observer.wait { speak.canPlay }
        guard speak.canSpeak, !speak.playback.captureOwned else {
            throw speakError("Speak did not come back after Save: \(speak.unavailableReason ?? "-")")
        }
        speak.playPause()
        try await observer.wait { speak.playback.phase == .playing }
        speak.playPause()
        try await observer.wait { speak.playback.phase == .paused }
        let afterSave = try await speakText("Speak works again after the take was saved.", speak: speak, observer: observer)
        guard afterSave.phase == .finished else { throw speakError("Speak failed after Save: \(afterSave)") }
        let interlock: RecordingJSON = .object([
            "take_id": .string(takeID), "during_capture": duringCapture, "after_stop": afterStop,
            "while_saving": whileSaving, "after_save_operation_id": .string(afterSave.operationID),
            "after_save_can_play": .boolean(true),
        ])

        // 8. While a fine-tune runs, Speak says why it waits and sends nothing.
        try setStubMode(["step_seconds": .number(speakSlowTrainingStepSeconds)])
        model.activateFineTuneSlot()
        guard model.fineTune.isActive, let job = model.fineTune.operationID else {
            throw speakError("Fine-tune model did not start a job: \(model.fineTune)")
        }
        // Cancelled once it trains, so its run record exists.
        try await observer.wait { model.fineTune.stage == .training || model.fineTune.phase.isTerminal }
        guard model.fineTune.phase == .running else { throw speakError("The fine-tune did not run: \(model.fineTune.statusLine ?? "-")") }
        let blockedReason = speak.unavailableReason
        let lastRequest = speak.request?.operationID
        speak.speak()
        guard blockedReason == RecordingSpeakModel.fineTuneActiveReason, speak.request?.operationID == lastRequest else {
            throw speakError("Speak did not wait for the fine-tune: \(blockedReason ?? "-")")
        }
        model.activateFineTuneSlot()
        try await observer.wait { model.fineTune.phase.isTerminal }
        guard model.fineTune.phase == .cancelled, model.fineTune.operationID == job else {
            throw speakError("The fine-tune did not end cancelled: \(model.fineTune.statusLine ?? "-")")
        }
        try setStubMode(["step_seconds": .number(speakTrainingStepSeconds)])
        guard speak.canSpeak else { throw speakError("Speak did not come back after the fine-tune: \(speak.unavailableReason ?? "-")") }
        let exclusion: RecordingJSON = .object([
            "fine_tune_operation_id": .string(job), "speak_reason_while_training": .string(blockedReason ?? ""),
            "fine_tune_help_while_speaking": .string(heldSlot.help), "fine_tune_enabled_while_speaking": .boolean(heldSlot.enabled),
            "speaking_operation_id": .string(generating),
        ])

        // 9. Save As: a byte copy outside the library.
        let saveFolder = directory.appendingPathComponent("save-as", isDirectory: true)
        try FileManager.default.createDirectory(at: saveFolder, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        let destination = saveFolder.appendingPathComponent("speech.wav")
        speak.selectClip(latestClip.id)
        guard speak.canSave else { throw speakError("Save As is unavailable: \(speak.saveUnavailableReason ?? "-")") }
        speak.saveClip(latestClip.id, to: destination)
        try await observer.wait { speak.savingClipID == nil }
        guard !speak.saveFailed, speak.saveNotice == "Saved speech.wav" else {
            throw speakError("Save As failed: \(speak.saveNotice ?? "-")")
        }

        // 10. Back on Record: the same regions.
        model.showPage(.record)
        try await settlePage(.record, model: model, host: host)
        let returned = measuredRegions(view: host)
        guard returned == baseline else { throw speakError("The Record page returned with different regions") }

        let clips: [RecordingJSON] = speak.clips.reversed().map { clip in
            .object([
                "operation_id": .string(clip.id), "text": .string(clip.text), "model": .string(clip.modelID),
                "model_title": .string(clip.modelTitle), "run_id": clip.runID.map(RecordingJSON.string) ?? .null,
                "reference_run_id": .string(clip.referenceRunID), "reference_prompt_id": .string(clip.referencePromptID),
                "relative_path": .string(clip.relativePath), "sha256": .string(clip.sha256),
                "frames": .integer(clip.frames), "sample_rate_hz": .integer(clip.sampleRateHz), "seconds": .number(clip.seconds),
                "phases": .array((phases[clip.id] ?? []).map(RecordingJSON.string)),
            ])
        }
        let report: RecordingJSON = .object([
            "suite": .string("speak"),
            "model_choices": .array(choices.map { .object(["id": .string($0.id), "title": .string($0.title)]) }),
            "reference_choices": .array([speakFirstReference, speakSecondReference].map(RecordingJSON.string)),
            "empty_text_reason": .string(emptyReason ?? ""),
            "requests": .object([
                "latest": .string(latest.operationID), "base": .string(base.operationID),
                "earlier": .string(earlier.operationID), "after_save": .string(afterSave.operationID),
            ]),
            "clips": .array(clips),
            "played": played,
            "cancelled": .object([
                "operation_id": .string(cancelled), "phases": .array((phases[cancelled] ?? []).map(RecordingJSON.string)),
                "cancelling_line": cancellingLine.map(RecordingJSON.string) ?? .null,
            ]),
            "deadline": .object([
                "operation_id": .string(late), "phases": .array((phases[late] ?? []).map(RecordingJSON.string)),
                "error_code": .string(failed.errorCode ?? ""), "status_line": .string(deadlineMessage),
            ]),
            "gpu_exclusion": exclusion,
            "interlock": interlock,
            "save_as": .object(["operation_id": .string(latestClip.id), "path": .string(destination.path),
                                "notice": .string(speak.saveNotice ?? "")]),
            "announcements": .array(announcements.map(RecordingJSON.string)),
            "fixed_geometry": .boolean(true), "speak_page_region_probes": .integer(0),
            // Measured here; the microphone is kept closed by the synthetic-PCM guard above.
            "visible_windows": .integer(Int64(NSApplication.shared.windows.filter { $0.isVisible }.count)),
        ])
        try report.transportData().write(to: directory.appendingPathComponent("speak-report.json"), options: .atomic)
        print(String(decoding: try report.transportData(), as: UTF8.self))
    }

    /// Type `text`, press Speak and wait for the request to end.
    @MainActor private static func speakText(_ text: String, speak: RecordingSpeakModel, observer: ReviewObserver) async throws -> RecordingSpeakRequest {
        let operation = try await startText(text, speak: speak, observer: observer, waitForRunning: false)
        try await observer.wait { speak.request.map { $0.operationID != operation || !$0.phase.isActive } ?? true }
        guard let request = speak.request, request.operationID == operation else { throw speakError("The request was replaced") }
        return request
    }

    /// Type `text`, press Speak and (by default) wait until the service runs it.
    @MainActor private static func startText(_ text: String, speak: RecordingSpeakModel, observer: ReviewObserver,
                                             waitForRunning: Bool = true) async throws -> String {
        speak.text = text
        guard speak.canSpeak else { throw speakError("Speak is unavailable: \(speak.unavailableReason ?? "-")") }
        speak.speak()
        guard let operation = speak.request?.operationID, speak.request?.phase == .queued, speak.isGenerating else {
            throw speakError("Speak did not send a request")
        }
        if waitForRunning {
            try await observer.wait { speak.request.map { $0.phase != .queued } ?? true }
            guard speak.request?.operationID == operation, speak.request?.phase == .running else {
                throw speakError("The request did not run: \(String(describing: speak.request))")
            }
        }
        return operation
    }

    /// Wait for the hosted container to show `page`.
    @MainActor private static func settlePage(_ page: RecordingPage, model: RecordingWorkspaceModel, host: NSView) async throws {
        await model.pageSwitchSettled()
        for _ in 0..<5 {
            await Task.yield()
            spinRunLoop()
            host.layoutSubtreeIfNeeded()
        }
        guard model.page == page else { throw speakError("The \(page.title) page is not shown") }
    }

    private static func speakError(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.SPEAK_HARNESS", message: message)
    }
}
