import AppKit
import SwiftUI

/// Native event handling preserves text, menus and dialogs before consulting transport policy.
@MainActor
public struct RecordingWorkspaceKeyboard: NSViewRepresentable {
    private let model: RecordingWorkspaceModel
    public init(model: RecordingWorkspaceModel) { self.model = model }
    public func makeNSView(context: Context) -> RecordingWorkspaceKeyboardView { RecordingWorkspaceKeyboardView(model: model) }
    public func updateNSView(_ view: RecordingWorkspaceKeyboardView, context: Context) { view.model = model }
    public static func dismantleNSView(_ view: RecordingWorkspaceKeyboardView, coordinator: ()) { view.uninstall() }
}

@MainActor
public final class RecordingWorkspaceKeyboardView: NSView {
    var model: RecordingWorkspaceModel
    private var monitor: Any?
    private var menuObservers: [NSObjectProtocol] = []
    private var menuDepth = 0
    init(model: RecordingWorkspaceModel) { self.model = model; super.init(frame: .zero) }
    required init?(coder: NSCoder) { nil }
    /// Whether the key monitor is installed (only while the workspace is in a window).
    var isMonitoring: Bool { monitor != nil }
    public override func hitTest(_ point: NSPoint) -> NSView? { nil }
    public override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow(); uninstall()
        guard window != nil else { return }
        for (notification, change) in [(NSMenu.didBeginTrackingNotification, 1), (NSMenu.didEndTrackingNotification, -1)] {
            menuObservers.append(NotificationCenter.default.addObserver(forName: notification, object: nil, queue: .main) { @Sendable [weak self] _ in
                MainActor.assumeIsolated { guard let self else { return }; self.menuDepth = max(0, min(64, self.menuDepth + change)) }
            })
        }
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            let consumed = MainActor.assumeIsolated { self?.handle(event) == nil }
            return consumed ? nil : event
        }
    }
    func uninstall() {
        if let monitor { NSEvent.removeMonitor(monitor); self.monitor = nil }
        for observer in menuObservers { NotificationCenter.default.removeObserver(observer) }
        menuObservers.removeAll(); menuDepth = 0
    }
    /// Also used by hosted tests with real NSEvent and native responder objects.
    public func handle(_ event: NSEvent) -> NSEvent? {
        guard let window, event.window === window else { return event }
        let responder = window.firstResponder
        let text = responder as? NSTextView
        let context = RecordingKeyboardContext(
            editingText: responder is NSTextField || (text.map { $0.isEditable || $0.selectedRange().length > 0 } ?? false),
            dialogOrPopup: menuDepth > 0 || model.reviewConfirmation != nil || window.attachedSheet != nil || NSApp.modalWindow != nil || responder is NSPopUpButton,
            waveformFocused: responder is RecordingWaveformFocusView)
        guard let action = RecordingKeyboardRouter.action(for: event, context: context) else { return event }
        model.handleKeyboard(action)
        return nil
    }
}

@MainActor
public struct RecordingWaveformKeyboardFocus: NSViewRepresentable {
    public init() {}
    public func makeNSView(context: Context) -> RecordingWaveformFocusView { RecordingWaveformFocusView() }
    public func updateNSView(_ view: RecordingWaveformFocusView, context: Context) {}
    public static func dismantleNSView(_ view: RecordingWaveformFocusView, coordinator: ()) { view.uninstall() }
}

@MainActor
public final class RecordingWaveformFocusView: NSView {
    private var monitor: Any?
    public override var acceptsFirstResponder: Bool { true }
    public override func hitTest(_ point: NSPoint) -> NSView? { nil }
    public override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow(); uninstall()
        focusRingType = .exterior
        guard window != nil else { return }
        monitor = NSEvent.addLocalMonitorForEvents(matching: .leftMouseDown) { [weak self] event in
            MainActor.assumeIsolated {
                guard let self, let window = self.window, event.window === window,
                      window.attachedSheet == nil, self.bounds.contains(self.convert(event.locationInWindow, from: nil)) else { return }
                window.makeFirstResponder(self)
            }
            return event
        }
    }
    public override func becomeFirstResponder() -> Bool { needsDisplay = true; return true }
    public override func resignFirstResponder() -> Bool { needsDisplay = true; return true }
    public override var focusRingMaskBounds: NSRect { bounds }
    public override func drawFocusRingMask() { bounds.fill() }
    func uninstall() { if let monitor { NSEvent.removeMonitor(monitor); self.monitor = nil } }
}
