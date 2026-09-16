import AppKit
import Combine
import CryptoKit
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI
import SwiftUI

extension RecordingHarness {
    @MainActor static func review(configurationURL: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard config.recorder.arguments.contains("--synthetic-pcm") else { throw reviewError("Review harness requires explicit synthetic PCM") }
        let backend = AVFoundationRecordingPlaybackBackend(mode: .offline)
        let heldBackend = RecordingHeldReviewBackend(base: backend)
        let startup = RecordingStartupHold()
        let spans = RecordingReviewSpanLog()
        let model = RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false }, reviewBackend: heldBackend,
                                            diagnosticSink: spans.sink, beforeCapture: { await startup.pass() })
        defer { model.close() }
        let observer = ReviewObserver(model)
        await model.connect()
        try await observer.wait { model.canRecord && model.state.historyCompleteness == "complete" }
        model.selectPrompt("reading-one")
        try await observer.wait { model.state.prompt?.id == "reading-one" && !model.state.navigationPending }
        model.record()
        try await observer.wait { model.waveform.snapshot?.writtenFrames == 4801 }
        model.stop()
        try await observer.wait { model.canSave && model.selectedReview != nil && !model.review.loading }
        guard let source = model.selectedReview, let takeID = model.state.takeID else { throw reviewError("Capture did not authorize its stopped original") }
        let originalURL = config.assetRoot.appendingPathComponent(source.playback.relativePath)
        let original = try Data(contentsOf: originalURL)
        let selection = model.state.selection
        guard source.playback.frameCount == 4801, model.review.playback.phase == .stopped else { throw reviewError("Review count or non-autoplay invariant failed") }
        model.review.setStartText("100"); model.review.setEndText("2100")
        model.requestRetake()
        guard model.reviewConfirmation != nil, model.state.selection == selection else { throw reviewError("Unsaved trim must require an explicit choice before Retake") }
        model.cancelReviewConfirmation()
        model.saveTrim()
        try await observer.wait { model.selectedReview?.editRevision == 1 && model.state.phase == .draftReview }
        try await observer.wait { model.reviewDerivativeState == "ready" }
        guard model.review.trimRange == 100..<2100, model.review.playback.phase == .stopped else { throw reviewError("Durable trim changed preview bounds or autoplayed") }
        model.playPauseSelected()
        try await observer.wait { model.review.playback.phase == .playing }
        let rendered = try await backend.renderOffline(frames: 2000)
        guard rendered.count == 2000 else { throw reviewError("Actual offline player did not render the retained frame count") }
        for index in rendered.indices {
            let offset = 44 + (100 + index) * 3
            let bits = UInt32(original[offset]) | UInt32(original[offset + 1]) << 8 | UInt32(original[offset + 2]) << 16
            let sample = Int32(bitPattern: bits & 0x800000 != 0 ? bits | 0xff000000 : bits)
            guard rendered[index] == Float(sample) / 8_388_608 else { throw reviewError("Actual player rendered different retained PCM") }
        }
        try await model.review.playback.shutdown()
        model.requestDecision("remove"); model.confirmDecision()
        try await observer.wait { model.selectedDecision == "removed" && !model.state.navigationPending }
        model.requestDecision("restore")
        try await observer.wait { model.selectedDecision == "draft" && !model.state.navigationPending }
        model.requestDecision("reject"); model.confirmDecision()
        try await observer.wait { model.selectedDecision == "rejected" && !model.state.navigationPending }
        model.requestDecision("restore")
        try await observer.wait { model.selectedDecision == "draft" && !model.state.navigationPending }
        model.resetTrim()
        try await observer.wait { model.selectedReview?.editRevision == 2 && model.review.trimRange == 0..<4801 && model.state.phase == .draftReview }
        model.handleKeyboard(.save(advance: false))
        try await observer.wait { model.selectedDecision == "kept" && model.state.phase == .ready }
        guard model.state.prompt?.id == "reading-one", model.state.takeID == takeID else { throw reviewError("Command-S must retain its exact prompt and take") }
        await model.loadTakeHistory(reset: true)
        guard model.takeHistoryPromptID == "reading-one", model.takeHistory.contains(where: { $0.object?["take_id"] == .string(takeID) }) else { throw reviewError("Prompt-scoped dated history omitted its retained take") }
        model.requestRetake()
        try await observer.wait { model.state.takeID == nil && model.canRecord }
        model.selectTake(takeID)
        try await observer.wait { model.state.takeID == takeID && model.selectedReview != nil && !model.state.navigationPending }
        guard model.selectedDecision == "kept", model.review.playback.phase == .stopped,
              try Data(contentsOf: originalURL) == original else { throw reviewError("Retake/history review must retain the exact kept original without autoplay") }
        try await reviewPromptReturn(model: model, observer: observer, backend: heldBackend, startup: startup, spans: spans, keptTakeID: takeID)
        guard try Data(contentsOf: originalURL) == original else { throw reviewError("Prompt return must retain the exact kept original") }
        let host = NSHostingView(rootView: RecordingWorkspace(model: model))
        host.frame = CGRect(x: 0, y: 0, width: 1060, height: 700)
        host.appearance = NSAppearance(named: .aqua); host.layoutSubtreeIfNeeded()
        guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw reviewError("Review snapshot allocation failed") }
        host.cacheDisplay(in: host.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else { throw reviewError("Review snapshot encoding failed") }
        try png.write(to: directory.appendingPathComponent("actual-kept-review-light.png"), options: .atomic)
        try await accessibility(model: model, directory: directory)
        let report: RecordingJSON = .object([
            "suite": .string("review"), "take_id": .string(takeID), "source_path": .string(source.playback.relativePath),
            "source_sha256": .string(source.playback.sourceSHA256), "source_frames": .integer(4801), "offline_retained_frames": .integer(Int64(rendered.count)),
            "canonical_edit_revision": .integer(model.selectedReview?.editRevision ?? -1), "decision_revision": .integer(model.selectedReview?.decisionRevision ?? -1),
            "final_decision": .string(model.selectedDecision ?? "unknown"), "prompt_id": .string(model.state.prompt?.id ?? "unknown"),
            "checks": .array(["real-helper-capture", "real-service-review", "durable-trim", "owned-derivative-child", "offline-exact-player-frames", "remove-restore", "reject-restore", "reset-original", "command-s-no-advance", "independent-retake", "prompt-scoped-history", "prompt-return-selects-retained-take", "record-supersedes-loading-review", "unsaved-trim-choice", "original-bytes-unchanged"].map(RecordingJSON.string)),
        ])
        try JSONEncoder().encode(report).write(to: directory.appendingPathComponent("review-report.json"), options: .atomic)
        try await model.review.playback.shutdown()
        print(String(decoding: try JSONEncoder().encode(report), as: UTF8.self))
    }
    private static func reviewError(_ message: String) -> RecordingContractError { RecordingContractError(code: "RECORDING.REVIEW_HARNESS", message: message) }
}

@MainActor final class ReviewObserver {
    /// Canonical harness state checkpoint, including failures outside an error phase.
    static func checkpoint(_ model: RecordingWorkspaceModel, label: String) -> RecordingJSON {
        let state = model.state
        return .object([
            "checkpoint": .string(label), "uptime_ns": .integer(Int64(DispatchTime.now().uptimeNanoseconds)),
            "phase": .string(state.phase.rawValue), "status": .string(state.status),
            "error_code": state.errorCode.map(RecordingJSON.string) ?? .null,
            "history_completeness": .string(state.historyCompleteness), "history_revision": .integer(state.historyRevision),
            "registration_pending": .boolean(state.registrationPending), "navigation_pending": .boolean(state.navigationPending),
            "can_record": .boolean(model.canRecord), "can_save": .boolean(model.canSave),
            "save_advances": .boolean(model.saveAdvances), "save_title": .string(model.saveActionTitle),
            "take_id": state.takeID.map(RecordingJSON.string) ?? .null,
            "prompt_id": state.prompt.map { .string($0.id) } ?? .null, "selection": state.selection,
            "active_operation_id": state.activeOperationID.map(RecordingJSON.string) ?? .null,
            "decision": state.decision.map(RecordingJSON.string) ?? .null,
            "analysis": state.analysis.map(RecordingJSON.string) ?? .null,
            "written_frames": .integer(model.waveform.snapshot?.writtenFrames ?? 0),
            "waveform_ready": .boolean(model.waveformReady), "selected_review_present": .boolean(model.selectedReview != nil),
            "selected_review_take_id": model.selectedReview.map { .string($0.playback.takeID) } ?? .null,
            "review_loading": .boolean(model.review.loading),
            "review_error": model.review.failure.map { .string($0.message) } ?? .null,
            "playback_error": model.review.playback.failure.map { .string($0.message) } ?? .null,
            "recovery_error": model.recoverySupervisorError.map(RecordingJSON.string) ?? .null,
            "history_error": model.takeHistoryError.map(RecordingJSON.string) ?? .null,
        ])
    }
    let model: RecordingWorkspaceModel
    private var subscriptions: [AnyCancellable] = []
    private var continuation: CheckedContinuation<Void, Error>?
    private var predicate: (() -> Bool)?
    private var timeout: Task<Void, Never>?
    private let barrierSeconds: Int
    private var expectedFailureCode: String?
    private var toleratedFailureCode: String?
    init(_ model: RecordingWorkspaceModel, barrierSeconds: Int = 30) {
        self.model = model
        self.barrierSeconds = barrierSeconds
        for publisher in [model.objectWillChange.eraseToAnyPublisher(), model.review.objectWillChange.eraseToAnyPublisher(), model.review.playback.objectWillChange.eraseToAnyPublisher(), model.waveform.objectWillChange.eraseToAnyPublisher()] {
            subscriptions.append(publisher.sink { [weak self] _ in Task { @MainActor in self?.changed() } })
        }
    }
    func wait(until predicate: @escaping () -> Bool) async throws {
        if expectedFailureCode == nil, let failure = currentFailure { throw failure }
        if predicate() { return }
        try await withCheckedThrowingContinuation { continuation in
            precondition(self.continuation == nil)
            self.continuation = continuation; self.predicate = predicate
            timeout = Task { [weak self] in
                do { try await Task.sleep(for: .seconds(self?.barrierSeconds ?? 30)) } catch { return }
                guard let self else { return }
                finish(RecordingContractError(code: "RECORDING.REVIEW_TIMEOUT", message: "Actual review barrier timed out: \(model.state.status); \(model.review.failure?.message ?? "no review error")"))
            }
            changed()
        }
    }
    /// Wait for an expected failure. The ordinary barrier aborts on a recoverable error,
    /// which is exactly the state a killed or faulted peer produces.
    func waitForFailure(code: String) async throws {
        expectedFailureCode = code
        defer { expectedFailureCode = nil }
        try await wait {
            self.model.state.errorCode == code || self.model.review.failure?.code == code
        }
    }

    /// Wait across one named transient code. A recoverable phase or any other failure still
    /// aborts, so a benign terminal can never mask a dead end.
    func wait(tolerating code: String, until predicate: @escaping () -> Bool) async throws {
        toleratedFailureCode = code
        defer { toleratedFailureCode = nil }
        try await wait(until: predicate)
    }

    private func changed() {
        guard let predicate else { return }
        if expectedFailureCode == nil, let failure = currentFailure { finish(failure) }
        else if predicate() { finish(nil) }
    }
    /// A failed Save deliberately returns to draftReview. Its retained command error is
    /// terminal evidence even when the transport phase or caller predicate looks ready.
    private var currentFailure: RecordingContractError? {
        if let failure = model.review.failure ?? model.review.playback.failure { return failure }
        if let code = model.state.errorCode, code != toleratedFailureCode {
            return RecordingContractError(code: code, message: model.state.status,
                context: ["phase": model.state.phase.rawValue,
                    "history_completeness": model.state.historyCompleteness,
                    "take_id": model.state.takeID ?? "unselected"])
        }
        if model.state.phase == .recoverableError {
            return RecordingContractError(code: "RECORDING.REVIEW_FAILED", message: model.state.status)
        }
        return nil
    }
    private func finish(_ error: Error?) {
        guard let continuation else { return }
        self.continuation = nil; predicate = nil; timeout?.cancel(); timeout = nil
        if let error { continuation.resume(throwing: error) } else { continuation.resume() }
    }
}
