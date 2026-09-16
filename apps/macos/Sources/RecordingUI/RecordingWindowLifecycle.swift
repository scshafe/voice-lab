import AppKit
import SwiftUI

/// Window close waits for the same explicit trim decision and output shutdown as Quit.
@MainActor
public struct RecordingWindowLifecycle: NSViewRepresentable {
    let model: RecordingWorkspaceModel
    public init(model: RecordingWorkspaceModel) { self.model = model }
    public func makeCoordinator() -> Coordinator { Coordinator(model: model) }
    public func makeNSView(context: Context) -> LifecycleView {
        let view = LifecycleView(); view.coordinator = context.coordinator; return view
    }
    public func updateNSView(_ view: LifecycleView, context: Context) { context.coordinator.model = model; context.coordinator.attach(view.window) }
    public static func dismantleNSView(_ view: LifecycleView, coordinator: Coordinator) { coordinator.detach() }
    @MainActor public final class LifecycleView: NSView {
        weak var coordinator: Coordinator?
        public override func viewDidMoveToWindow() { super.viewDidMoveToWindow(); coordinator?.attach(window) }
        public override func hitTest(_ point: NSPoint) -> NSView? { nil }
    }
    @MainActor public final class Coordinator: NSObject, NSWindowDelegate {
        var model: RecordingWorkspaceModel
        weak var window: NSWindow?
        weak var previous: (any NSWindowDelegate)?
        private var closing = false
        init(model: RecordingWorkspaceModel) { self.model = model }
        func attach(_ next: NSWindow?) {
            guard window !== next else { return }
            detach(); guard let next else { return }
            window = next; previous = next.delegate; next.delegate = self
        }
        func detach() {
            if window?.delegate === self { window?.delegate = previous }
            window = nil; previous = nil
        }
        public func windowShouldClose(_ sender: NSWindow) -> Bool {
            if closing { return previous?.windowShouldClose?(sender) ?? true }
            model.closeHandler = { [weak self] in
                guard let self, let window else { return }
                closing = true; window.performClose(nil)
            }
            model.requestClose(); return false
        }
        public func windowWillClose(_ notification: Notification) { previous?.windowWillClose?(notification) }
        public func windowDidResize(_ notification: Notification) { previous?.windowDidResize?(notification) }
        public func windowDidBecomeKey(_ notification: Notification) { previous?.windowDidBecomeKey?(notification) }
        public func windowDidResignKey(_ notification: Notification) { previous?.windowDidResignKey?(notification) }
    }
}
