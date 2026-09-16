import AppKit
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI

extension RecordingHarness {
    @MainActor static func legacyReview(configurationURL: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard config.recorder.arguments.contains("--synthetic-pcm") else { throw legacyError("Explicit synthetic helper required") }
        let fixture = try RecordingJSON.parse(Data(contentsOf: configurationURL.deletingLastPathComponent().appendingPathComponent("legacy-fixture.json")))
        let model = RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false }, reviewBackend: AVFoundationRecordingPlaybackBackend(mode: .offline))
        defer { model.close() }
        let observer = ReviewObserver(model)
        await model.connect()
        try await observer.wait { model.canRecord && model.state.historyCompleteness == "complete" }
        model.selectPrompt("reading-one")
        try await observer.wait { model.state.prompt?.id == "reading-one" && !model.state.navigationPending }
        // The explicit selection already names the retained legacy take and loads it; a manual
        // reselection here would race that load and could reset the trim fields below.
        let autoSelectedTakeID = model.state.takeID
        await model.loadTakeHistory(reset: true)
        guard model.takeHistory.count == 1, let takeID = model.takeHistory.first?.object?["take_id"]?.string else { throw legacyError("Alias-aware legacy history must expose one logical selected take") }
        guard autoSelectedTakeID == takeID else { throw legacyError("Returning to a recorded legacy prompt must select its retained take") }
        try await observer.wait { model.selectedReview?.playback.takeID == takeID && !model.review.loading && !model.state.navigationPending }
        guard let selected = model.selectedReview, selected.originalKind == fixture.object?["original_kind"]?.string,
              selected.playback.frameCount == 960, !model.canSave else { throw legacyError("Legacy source must retain its actual identity and current-draft Save guard") }
        let file = config.assetRoot.appendingPathComponent(selected.playback.relativePath)
        let original = try Data(contentsOf: file)
        model.review.setStartText("17"); model.review.setEndText("313"); model.saveTrim()
        try await observer.wait { model.selectedReview?.editRevision == 1 && model.state.phase == .ready }
        try await observer.wait { model.legacyReviewStatus == "Legacy action completed · original retained" }
        guard model.review.trimRange == 17..<313, model.review.playback.phase == .stopped else { throw legacyError("Completed legacy derivative changed the preview range or autoplayed") }
        for action in ["remove", "restore", "reject", "restore"] {
            model.requestDecision(action)
            if action != "restore" { model.confirmDecision() }
            let decision = action == "remove" ? "removed" : action == "reject" ? "rejected" : "kept"
            try await observer.wait { model.selectedDecision == decision && !model.state.navigationPending }
            try await observer.wait { model.legacyReviewStatus == "Legacy action completed · original retained" }
        }
        model.resetTrim()
        try await observer.wait { model.selectedReview?.editRevision == 2 && model.review.trimRange == 0..<960 }
        try await observer.wait { model.legacyReviewStatus == "Legacy action completed · original retained" }
        guard try Data(contentsOf: file) == original, model.review.playback.phase == .stopped else { throw legacyError("Legacy review must preserve original PCM and stopped transport") }
        let report: RecordingJSON = .object(["suite": .string("review-legacy"), "take_id": .string(takeID), "original_kind": .string(selected.originalKind), "source_sha256": .string(selected.playback.sourceSHA256), "source_frames": .integer(960), "edit_revision": .integer(model.selectedReview?.editRevision ?? -1), "final_decision": .string(model.selectedDecision ?? "unknown"), "original_unchanged": .boolean(true), "actual_worker_terminal_events": .boolean(true)])
        try JSONEncoder().encode(report).write(to: directory.appendingPathComponent("legacy-review-report.json"), options: .atomic)
        print(String(decoding: try JSONEncoder().encode(report), as: UTF8.self))
        try await model.review.playback.shutdown()
    }
    private static func legacyError(_ text: String) -> RecordingContractError { .init(code: "RECORDING.LEGACY_REVIEW_HARNESS", message: text) }
}
