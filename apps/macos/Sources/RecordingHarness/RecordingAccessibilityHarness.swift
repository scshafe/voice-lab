import AppKit
import ApplicationServices
import Foundation
import RecordingContracts
import RecordingUI
import SwiftUI

extension RecordingHarness {
    /// The Speak page's controls, which the accessibility gate requires on that page only.
    static let speakIdentifiers = ["speak.text", "speak.model", "speak.reference", "speak.generate", "speak.cancel",
                                   "speak.play-pause", "speak.position", "speak.clips", "speak.save-as", "speak.status"]
    static let recordIdentifiers = ["library.continueMissing", "library.search", "library.filter", "library.labels", "library.coverage", "trim.start", "trim.end", "playback.play-pause", "transport.record-stop", "transport.save-next", "transport.fine-tune", "recording.labels", "recording.script", "recording.instructions", "recording.status"]

    /// Hosts the window's page container: the Record page (its identifiers, labels, roles and
    /// region geometry), then the Speak page (its identifiers, no Record machinery), then the
    /// Record page again with byte-identical regions, in both appearances.
    @MainActor static func accessibility(model: RecordingWorkspaceModel, directory: URL) async throws {
        NSApp.setActivationPolicy(.accessory)
        NSApp.finishLaunching()
        registerLightAppearancePreference()
        let size = CGSize(width: 1060, height: 700)
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.titled, .resizable], backing: .buffered, defer: true)
        let host = NSHostingView(rootView: RecordingPageContainer(model: model).accessibilityElement(children: .contain).environment(\.recordingReduceMotionOverride, true))
        window.contentView = host; host.frame = CGRect(origin: .zero, size: size)
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
            host.appearance = NSAppearance(named: name)
            try await showPage(.record, model: model, host: host)
            let recordRegions = measuredRegions(view: host)
            try validateRegions(recordRegions, size: size, focusMode: model.focusMode, baseline: nil)
            try requireKeyboardMonitor(true, in: host, page: .record)
            let records = accessibleRecords()
            let ids = Set(records.compactMap { $0.object?["identifier"]?.string })
            // The review harness leaves its spoken prompt `reading-one` selected, so both script sections must exist.
            guard model.state.prompt?.id == "reading-one", model.state.prompt?.displayPrompt != nil else {
                throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Accessibility harness requires its spoken prompt reading-one")
            }
            for required in recordIdentifiers {
                guard ids.contains(required) else { throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Actual native accessibility tree omitted \(required)") }
            }
            guard ids.isDisjoint(with: speakIdentifiers) else {
                throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "The Record page exposes Speak controls")
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
            let speakRecords = try await speakPage(model: model, host: host, style: style, directory: directory)
            // Back on Record: the same regions, to the point, and the key monitor again.
            try await showPage(.record, model: model, host: host)
            let returned = measuredRegions(view: host)
            guard returned == recordRegions else {
                throw RecordingContractError(code: "RECORDING.GEOMETRY_SHIFT", message: "The Record page returned with different regions", context: ["appearance": style])
            }
            try requireKeyboardMonitor(true, in: host, page: .record)
            appearances.append(.object(["appearance": .string(style), "reduce_motion": .boolean(true), "elements": .array(records),
                                        "speak_elements": .array(speakRecords),
                                        "record_regions": .object(recordRegions.mapValues { .array([$0.minX, $0.minY, $0.width, $0.height].map { .number(Double($0)) }) }),
                                        "text_contrast": .object(contrast.mapValues(RecordingJSON.number))]))
        }
        window.contentView = nil
        let report = RecordingJSON.object(["suite": .string("accessibility"), "shown_window": .boolean(true), "activated_application": .boolean(false), "voiceover_announcements_posted": .boolean(false),
                                           "pages": .array([.string("record"), .string("speak")]), "appearances": .array(appearances)])
        try JSONEncoder().encode(report).write(to: directory.appendingPathComponent("accessibility-report.json"), options: .atomic)
    }
    /// The Speak page in the hosted container: its controls are named, none of the Record page's
    /// controls, regions or key monitor remain, and it renders in the forced appearance.
    @MainActor private static func speakPage(model: RecordingWorkspaceModel, host: NSView, style: String, directory: URL) async throws -> [RecordingJSON] {
        try await showPage(.speak, model: model, host: host)
        guard measuredRegions(view: host).isEmpty else {
            throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "The Speak page carries Record region probes")
        }
        try requireKeyboardMonitor(false, in: host, page: .speak)
        let records = accessibleRecords()
        let ids = Set(records.compactMap { $0.object?["identifier"]?.string })
        for required in speakIdentifiers where !ids.contains(required) {
            throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "The Speak page omitted \(required)")
        }
        guard ids.isDisjoint(with: recordIdentifiers) else {
            throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "The Speak page still exposes Record controls: \(ids.intersection(recordIdentifiers).sorted())")
        }
        let labels = Dictionary(records.compactMap { record -> (String, String)? in
            guard let identifier = record.object?["identifier"]?.string, !identifier.isEmpty,
                  let label = record.object?["label"]?.string, !label.isEmpty else { return nil }
            return (identifier, label)
        }, uniquingKeysWith: { first, _ in first })
        let expected = ["speak.text": "Text to speak", "speak.model": "Model", "speak.reference": "Reference clip",
                        "speak.generate": model.speak.isGenerating ? "Generating speech" : "Speak",
                        "speak.cancel": "Cancel speech generation", "speak.play-pause": "Play speech",
                        "speak.position": "Playback position", "speak.clips": "Clips spoken this session",
                        "speak.save-as": "Save the selected clip as", "speak.status": "Speak status"]
        let wrong = expected.filter { labels[$0.key] != $0.value }
        guard wrong.isEmpty else {
            throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Speak controls are misnamed: \(wrong.keys.sorted().map { "\($0)=\(labels[$0] ?? "-")" })")
        }
        guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Hosted Speak snapshot unavailable") }
        host.cacheDisplay(in: host.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else { throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "Hosted Speak PNG unavailable") }
        let file = "actual-speak-\(style)-reduce-motion.png"
        try data.write(to: directory.appendingPathComponent(file), options: .atomic)
        try requireRenderedAppearance(bitmap, dark: style == "dark", context: file)
        return records
    }

    /// Switch the hosted container's page and let SwiftUI apply it.
    @MainActor private static func showPage(_ page: RecordingPage, model: RecordingWorkspaceModel, host: NSView) async throws {
        if model.page != page {
            guard model.canSwitchPage else {
                throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "The page cannot switch: \(model.pageSwitchUnavailableReason ?? "")")
            }
            model.showPage(page)
            await model.pageSwitchSettled()
        }
        for _ in 0..<5 {
            await Task.yield()
            spinRunLoop()
            host.layoutSubtreeIfNeeded()
        }
        guard model.page == page else {
            throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "The \(page.title) page is not shown")
        }
    }

    /// One short turn of the main run loop, where SwiftUI applies a pending page change.
    @MainActor static func spinRunLoop() {
        RunLoop.current.run(until: Date().addingTimeInterval(0.01))
    }

    /// The Record key monitor exists exactly while the Record page is mounted.
    @MainActor private static func requireKeyboardMonitor(_ expected: Bool, in root: NSView, page: RecordingPage) throws {
        func count(_ view: NSView) -> Int { (view is RecordingWorkspaceKeyboardView ? 1 : 0) + view.subviews.map(count).reduce(0, +) }
        guard (count(root) == 1) == expected, count(root) <= 1 else {
            throw RecordingContractError(code: "RECORDING.ACCESSIBILITY_HARNESS", message: "The Record key monitor is \(expected ? "missing from" : "still on") the \(page.title) page")
        }
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
