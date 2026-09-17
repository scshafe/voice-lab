import AppKit
import ApplicationServices
import Foundation
import RecordingContracts
import RecordingUI
import SwiftUI

extension RecordingHarness {
    @MainActor static func accessibility(model: RecordingWorkspaceModel, directory: URL) async throws {
        NSApp.setActivationPolicy(.accessory)
        NSApp.finishLaunching()
        registerLightAppearancePreference()
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1060, height: 700), styleMask: [.titled, .resizable], backing: .buffered, defer: true)
        let host = NSHostingView(rootView: RecordingWorkspace(model: model).accessibilityElement(children: .contain).environment(\.recordingReduceMotionOverride, true))
        window.contentView = host; host.frame = CGRect(x: 0, y: 0, width: 1060, height: 700)
        let accessibilityRequest = await Task.detached {
            let application = AXUIElementCreateApplication(getpid())
            AXUIElementSetMessagingTimeout(application, 0.5)
            var value: CFTypeRef?
            return AXUIElementCopyAttributeValue(application, kAXRoleAttribute as CFString, &value).rawValue
        }.value
        try String(accessibilityRequest).write(to: directory.appendingPathComponent("ax-request.txt"), atomically: true, encoding: .utf8)
        window.orderBack(nil)
        defer { window.orderOut(nil); window.contentView = nil; window.close() }
        await Task.yield()
        var appearances: [RecordingJSON] = []
        for name in [NSAppearance.Name.aqua, .darkAqua] {
            host.appearance = NSAppearance(named: name); host.layoutSubtreeIfNeeded()
            let records = accessibleRecords()
            let ids = Set(records.compactMap { $0.object?["identifier"]?.string })
            // The review harness leaves its spoken prompt `reading-one` selected, so both script sections must exist.
            guard model.state.prompt?.id == "reading-one", model.state.prompt?.displayPrompt != nil else {
                throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Accessibility harness requires its spoken prompt reading-one")
            }
            for required in ["library.continueMissing", "library.search", "library.filter", "library.labels", "library.coverage", "trim.start", "trim.end", "playback.play-pause", "transport.record-stop", "transport.save-next", "transport.fine-tune", "recording.labels", "recording.script", "recording.instructions", "recording.status"] {
                guard ids.contains(required) else { throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Actual native accessibility tree omitted \(required)") }
            }
            // No fine-tune job runs here, so the footer slot is Fine-tune model, named in words; the
            // status region is one named element; the coverage count is spoken as a sentence.
            // Static text carries its spoken words as its value rather than a title or description.
            let labels = Dictionary(records.compactMap { record -> (String, String)? in
                guard let identifier = record.object?["identifier"]?.string, !identifier.isEmpty,
                      let label = record.object?["label"]?.string, let value = record.object?["value"]?.string else { return nil }
                return (identifier, label.isEmpty ? value : label)
            }, uniquingKeysWith: { first, _ in first })
            let coverage = model.libraryCoverage.spokenText
            guard model.fineTuneSlot.identifier == "transport.fine-tune", labels["transport.fine-tune"] == "Fine-tune model",
                  labels["recording.status"] == "Status", labels["library.coverage"] == coverage,
                  !ids.contains("transport.cancel-fine-tune") else {
                throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Fine-tune, status or coverage labels are missing: \(labels.filter { ["transport.fine-tune", "recording.status", "library.coverage"].contains($0.key) })")
            }
            // §6.4: the Labels menu is never a text field, and the search field is the only text field among the
            // library controls, so the search field stays the sidebar's first editable field.
            let libraryRoles = Dictionary(records.compactMap { record -> (String, String)? in
                guard let identifier = record.object?["identifier"]?.string, identifier.hasPrefix("library."),
                      let role = record.object?["role"]?.string, !role.isEmpty else { return nil }
                return (identifier, role)
            }, uniquingKeysWith: { first, _ in first })
            guard let labelsRole = libraryRoles["library.labels"], !["AXTextField", "AXTextArea"].contains(labelsRole),
                  Set(libraryRoles.filter { $0.value == "AXTextField" }.keys) == ["library.search"] else {
                throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Library controls expose unexpected roles: \(libraryRoles)")
            }
            guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Hosted accessibility snapshot unavailable") }
            host.cacheDisplay(in: host.bounds, to: bitmap)
            guard let data = bitmap.representation(using: .png, properties: [:]) else { throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Hosted accessibility PNG unavailable") }
            let style = name == .aqua ? "light" : "dark"
            try data.write(to: directory.appendingPathComponent("actual-review-\(style)-reduce-motion.png"), options: .atomic)
            // TA1: the registered Light preference must not turn the forced dark pass light, and hosting the
            // workspace in an ordered window must not have set a window or application appearance.
            try requireRenderedAppearance(bitmap, dark: name == .darkAqua, context: "actual-review-\(style)-reduce-motion.png")
            try requireApplicationAppearanceUntouched()
            guard window.appearance == nil else { throw RecordingContractError(code: "RECORDING.SNAPSHOT_APPEARANCE", message: "Hosting the workspace set the window appearance") }
            let contrast = contrastValues(appearance: host.appearance!)
            try JSONEncoder().encode(contrast).write(to: directory.appendingPathComponent("contrast-\(style).json"), options: .atomic)
            guard contrast.allSatisfy({ $0.value >= 4.5 }) else { throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Resolved text colors fall below 4.5:1 contrast") }
            appearances.append(.object(["appearance": .string(style), "reduce_motion": .boolean(true), "elements": .array(records), "text_contrast": .object(contrast.mapValues(RecordingJSON.number))]))
        }
        window.contentView = nil
        let report = RecordingJSON.object(["suite": .string("accessibility"), "shown_window": .boolean(true), "activated_application": .boolean(false), "voiceover_announcements_posted": .boolean(false), "appearances": .array(appearances)])
        try JSONEncoder().encode(report).write(to: directory.appendingPathComponent("accessibility-report.json"), options: .atomic)
    }
    /// Own-process AX calls enter SwiftUI directly, so these public reads stay on MainActor.
    @MainActor private static func accessibleRecords() -> [RecordingJSON] {
        let application = AXUIElementCreateApplication(getpid())
        AXUIElementSetMessagingTimeout(application, 1)
        var records: [RecordingJSON] = []
        func attribute(_ element: AXUIElement, _ key: CFString) -> CFTypeRef? {
            var value: CFTypeRef?
            guard AXUIElementCopyAttributeValue(element, key, &value) == .success else { return nil }
            return value
        }
        func visit(_ element: AXUIElement, depth: Int) {
            guard depth < 30, records.count < 5000 else { return }
            // A control named only by its accessibility label has an empty title; use the description then.
            let title = attribute(element, kAXTitleAttribute as CFString) as? String
            let label = (title?.isEmpty == false ? title : nil) ?? attribute(element, kAXDescriptionAttribute as CFString) as? String ?? ""
            let identifier = attribute(element, kAXIdentifierAttribute as CFString) as? String ?? ""
            let role = attribute(element, kAXRoleAttribute as CFString) as? String ?? ""
            let value = attribute(element, kAXValueAttribute as CFString) as? String ?? ""
            var point = CGPoint.zero, size = CGSize.zero
            if let value = attribute(element, kAXPositionAttribute as CFString), CFGetTypeID(value) == AXValueGetTypeID() {
                AXValueGetValue(value as! AXValue, .cgPoint, &point)
            }
            if let value = attribute(element, kAXSizeAttribute as CFString), CFGetTypeID(value) == AXValueGetTypeID() {
                AXValueGetValue(value as! AXValue, .cgSize, &size)
            }
            records.append(.object(["label": .string(label), "value": .string(value), "identifier": .string(identifier), "role": .string(role), "frame": .array([point.x, point.y, size.width, size.height].map { .number(Double($0)) })]))
            for child in attribute(element, kAXChildrenAttribute as CFString) as? [AXUIElement] ?? [] { visit(child, depth: depth + 1) }
        }
        visit(application, depth: 0)
        return records
    }
    @MainActor private static func contrastValues(appearance: NSAppearance) -> [String: Double] {
        var result: [String: Double] = [:]
        appearance.performAsCurrentDrawingAppearance {
            for (surface, backgroundColor) in [("text", NSColor.textBackgroundColor), ("window", NSColor.windowBackgroundColor), ("control", NSColor.controlBackgroundColor)] {
                let background = backgroundColor.usingColorSpace(.sRGB)!
                for (name, color) in [("primary", NSColor.labelColor), ("supporting", RecordingPalette.supportingNSColor)] {
                    let text = color.usingColorSpace(.sRGB)!
                    func luminance(_ values: [CGFloat]) -> Double {
                        let linear = values.map { value -> Double in let v = Double(value); return v <= 0.04045 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4) }
                        return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
                    }
                    let bg = [background.redComponent, background.greenComponent, background.blueComponent]
                    let fg = zip([text.redComponent, text.greenComponent, text.blueComponent], bg).map { $0 * text.alphaComponent + $1 * (1 - text.alphaComponent) }
                    let a = luminance(fg), b = luminance(bg)
                    result[name + "." + surface] = (max(a, b) + 0.05) / (min(a, b) + 0.05)
                }
            }
        }
        return result
    }
}
