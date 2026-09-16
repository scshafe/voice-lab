import AppKit
import Foundation

/// Canonical ownership of workspace sleep/wake notifications. No callback starts input.
@MainActor
public final class RecordingSystemLifecycle: NSObject {
    private let center: NotificationCenter
    private weak var model: RecordingWorkspaceModel?

    public init(model: RecordingWorkspaceModel, center: NotificationCenter = NSWorkspace.shared.notificationCenter) {
        self.model = model; self.center = center
        super.init()
        center.addObserver(self, selector: #selector(willSleep), name: NSWorkspace.willSleepNotification, object: nil)
        center.addObserver(self, selector: #selector(didWake), name: NSWorkspace.didWakeNotification, object: nil)
    }

    @objc private func willSleep(_ notification: Notification) { model?.handleSystemSleep() }
    @objc private func didWake(_ notification: Notification) { model?.handleSystemWake() }
    public func close() { center.removeObserver(self); model = nil }
    deinit { center.removeObserver(self) }
}
