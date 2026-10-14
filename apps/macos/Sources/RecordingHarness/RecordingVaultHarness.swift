import AppKit
import Combine
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI
import SwiftUI

/// @canonical native_vault_harness -- visible vault states driven by real synthetic copies.
extension RecordingHarness {
    @MainActor static func vault(configurationURL: URL, directory: URL) async throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let config = try RecordingApplicationConfiguration.load(privateFile: configurationURL)
        guard config.recorder.arguments.contains("--synthetic-pcm") else { throw vaultFailure("Synthetic PCM is required") }
        let model = RecordingWorkspaceModel(configuration: config, permissionStatus: { .granted }, permissionRequest: { false },
                                            reviewBackend: AVFoundationRecordingPlaybackBackend(mode: .offline),
                                            speechBackend: AVFoundationRecordingPlaybackBackend(mode: .offline))
        defer { model.close() }
        let observer = ReviewObserver(model, barrierSeconds: 90)
        var states: [String] = []
        let watcher = model.$vault.sink { value in
            if let state = value.state, states.last != state { states.append(state) }
        }
        defer { watcher.cancel() }
        await model.connect()
        try await observer.wait { model.canRecord && model.speak.runsKnown && model.vault.state == "up-to-date" }
        // SwiftUI publishes accessibility nodes only after launch and attachment to a window.
        // This owned window is never ordered on screen.
        NSApplication.shared.finishLaunching()
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1060, height: 700),
                              styleMask: [.titled, .resizable], backing: .buffered, defer: true)
        // Swift owns the window: `close()` must not release it too (a double release on macOS 27).
        window.isReleasedWhenClosed = false
        let host = NSHostingView(rootView: RecordingPageContainer(model: model))
        window.contentView = host
        defer { window.contentView = nil; window.close() }
        host.frame = CGRect(x: 0, y: 0, width: 1060, height: 700)
        for _ in 0..<5 { await Task.yield(); spinRunLoop(); host.layoutSubtreeIfNeeded() }
        let baseline = measuredRegions(view: host)
        try validateRegions(baseline, size: host.frame.size, focusMode: false, baseline: nil)
        let initialGeneration = model.vault.generation
        states.removeAll()
        model.selectPrompt(speakRecordedPrompt)
        try await observer.wait { model.state.prompt?.id == speakRecordedPrompt && !model.state.navigationPending }
        model.record()
        try await observer.wait { model.waveform.snapshot?.writtenFrames == 4801 }
        model.stop()
        try await observer.wait { model.canSave && model.selectedReview != nil && !model.review.loading }
        guard let takeID = model.state.takeID else { throw vaultFailure("The fixture take has no id") }
        model.save(advance: false)
        try await observer.wait { model.state.phase == .ready && model.state.decision == "kept" }
        try await observer.wait { model.vault.state == "up-to-date" && model.vault.generation > initialGeneration }
        guard states.contains("waiting") || states.contains("copying") else { throw vaultFailure("No pending copy was shown") }
        let copiedGeneration = model.vault.generation
        let copiedLine = try await requireVaultLabel("Vault · up to date", in: host)
        // Python stops and restarts the actual loopback listener; no fabricated status events.
        try Data().write(to: directory.appendingPathComponent("stop-vault"))
        try await observer.wait { model.vault.state == "unreachable" }
        guard let since = model.vault.unreachableSince else { throw vaultFailure("The outage has no start time") }
        let outage = try await requireVaultLabel(outageExpectation(since), in: host)
        guard model.canRecord else { throw vaultFailure("Capture became unavailable during the outage") }
        try Data().write(to: directory.appendingPathComponent("start-vault"))
        try await observer.wait { model.vault.state == "up-to-date" }
        let recovered = try await requireVaultLabel("Vault · up to date", in: host)
        model.showPage(.speak)
        await model.pageSwitchSettled()
        model.speak.text = "An archived model must be restored before it speaks."
        let blocker = "Archived — restore with voice-lab finetune restore-run --run \(speakLatestRun)"
        guard model.speak.unavailableReason == blocker,
              model.speak.selectedModel?.title.hasSuffix(" · Archived") == true else { throw vaultFailure("Archived Latest has no restore instruction") }
        model.speak.speak()
        guard model.speak.request == nil else { throw vaultFailure("An archived model submitted synthesis") }
        model.speak.selectedModelID = "base"
        guard model.speak.canSpeak else { throw vaultFailure("Base cannot use the retained reference dataset") }
        model.showPage(.record)
        await model.pageSwitchSettled()
        for _ in 0..<5 { await Task.yield(); spinRunLoop(); host.layoutSubtreeIfNeeded() }
        guard measuredRegions(view: host) == baseline else { throw vaultFailure("Vault states changed Record region geometry") }
        let report: RecordingJSON = .object([
            "suite": .string("vault"), "take_id": .string(takeID),
            "generation": .integer(copiedGeneration), "states": .array(states.map(RecordingJSON.string)),
            "copied_line": .string(copiedLine), "outage_line": .string(outage), "recovered_line": .string(recovered),
            "status_observation": .string("hosted library.vault-status accessibility value"),
            "archived_blocker": .string(blocker), "base_allowed": .boolean(true),
            "fixed_geometry": .boolean(true),
            "visible_windows": .integer(Int64(NSApplication.shared.windows.filter { $0.isVisible }.count)),
        ])
        try report.transportData().write(to: directory.appendingPathComponent("vault-report.json"), options: .atomic)
        print(String(decoding: try report.transportData(), as: UTF8.self))
    }

    /// Read the mounted text node itself; model state alone cannot prove the line was drawn.
    @MainActor private static func requireVaultLabel(_ expected: String, in host: NSView) async throws -> String {
        var observed = "missing library.vault-status"
        for _ in 0..<50 {
            await Task.yield()
            spinRunLoop()
            host.layoutSubtreeIfNeeded()
            if let node = accessibilityElement("library.vault-status", in: host),
               let element = node.element as? NSObject,
               element.responds(to: NSSelectorFromString("accessibilityValue")),
               let value = element.value(forKey: "accessibilityValue") as? String {
                observed = value
                if value == expected { return value }
            }
        }
        throw vaultFailure("Hosted library.vault-status expected '\(expected)', observed '\(observed)'")
    }

    /// Independently derive the expected local minute from the service's raw timestamp.
    private static func outageExpectation(_ timestamp: String) throws -> String {
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = parser.date(from: timestamp)
        if date == nil {
            parser.formatOptions = [.withInternetDateTime]
            date = parser.date(from: timestamp)
        }
        guard let date else { throw vaultFailure("The outage timestamp is invalid") }
        let components = Calendar.current.dateComponents([.hour, .minute], from: date)
        guard let hour = components.hour, let minute = components.minute else { throw vaultFailure("The outage has no local minute") }
        return "Vault unreachable since " + String(format: "%02d:%02d", hour, minute)
    }

    private static func vaultFailure(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.VAULT_HARNESS", message: message)
    }
}
