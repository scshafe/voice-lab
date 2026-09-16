import AppKit
import RecordingUI
import SwiftUI

struct RecordingApp: App {
    @NSApplicationDelegateAdaptor(RecordingApplicationDelegate.self) private var delegate
    @StateObject private var session = RecordingApplicationSession()
    // The stored View ▸ Appearance choice is applied application-wide from the scene, never inside
    // RecordingWorkspace, so harness-hosted workspaces keep their forced appearance. The verification
    // entry never builds this scene and therefore never reads the preference.
    @StateObject private var appearance = RecordingAppearanceController(defaults: .standard)
    var body: some Scene {
        Window("Cole Voice Recorder", id: "recording") {
            RecordingWorkspace(model: session.model)
                .background(RecordingWindowLifecycle(model: session.model))
                .task { await session.start(); delegate.model = session.model }
        }
        .defaultSize(width: 1060, height: 700)
        .commands {
            // View ▸ Appearance ▸ System / Light / Dark; choosing System restores the system appearance at once.
            CommandGroup(after: .toolbar) {
                Picker("Appearance", selection: $appearance.preference) {
                    ForEach(RecordingAppearancePreference.allCases) { Text($0.title).tag($0) }
                }
            }
        }
    }
}

/// Termination is deferred until explicit trim choice and confirmed native output shutdown.
@MainActor
final class RecordingApplicationDelegate: NSObject, NSApplicationDelegate {
    weak var model: RecordingWorkspaceModel?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model else { return .terminateNow }
        model.closeCancellationHandler = { sender.reply(toApplicationShouldTerminate: false) }
        model.closeHandler = { sender.reply(toApplicationShouldTerminate: true) }
        model.requestClose()
        return .terminateLater
    }
}
