import AppKit
import Combine
import Foundation
import RecordingContracts
import RecordingCore
import RecordingUI

/// One application workspace owns its long-lived local channels.
@MainActor
final class RecordingApplicationSession: ObservableObject {
    @Published private(set) var model = RecordingWorkspaceModel(unavailable: "Preparing recording workspace…")
    private var started = false
    private var announcements: AnyCancellable?

    func start() async {
        guard !started else { return }; started = true
        do {
            let url = try RecordingApplicationConfiguration.configurationURL()
            let configuration = try await Task.detached(priority: .userInitiated) {
                try RecordingApplicationConfiguration.load(privateFile: url)
            }.value
            await start(configuration: configuration)
        } catch {
            model = RecordingWorkspaceModel(unavailable: (error as? RecordingContractError)?.message ?? "Application configuration could not be loaded")
        }
    }

    /// The verification entry uses the same configured session after rejecting any real-input configuration.
    func start(configuration: RecordingApplicationConfiguration) async {
        do {
            let synthetic = configuration.recorder.arguments.contains("--synthetic-pcm")
            let connected = try RecordingWorkspaceModel(configuration: configuration,
                permissionStatus: { synthetic ? .granted : RecordingMicrophonePermission.current() },
                permissionRequest: { if synthetic { return false }; return await RecordingMicrophonePermission.request() },
                reviewBackend: AVFoundationRecordingPlaybackBackend(mode: synthetic ? .offline : .device))
            model = connected
            connected.observeSystemLifecycle(center: NSWorkspace.shared.notificationCenter)
            // Automated synthetic fixtures never post announcements or invoke VoiceOver.
            if !configuration.recorder.arguments.contains("--synthetic-pcm") {
                announcements = connected.$state.map(\.status).removeDuplicates().dropFirst().sink { [weak connected] _ in
                    guard let connected, let window = NSApp.mainWindow else { return }
                    NSAccessibility.post(element: window, notification: .announcementRequested, userInfo: [.announcement: connected.state.status, .priority: NSAccessibilityPriorityLevel.medium.rawValue])
                }
            }
            await connected.connect()
        } catch {
            model = RecordingWorkspaceModel(unavailable: (error as? RecordingContractError)?.message ?? "Application configuration could not be loaded")
        }
    }
}
