import AppKit
import RecordingUI
import SwiftUI
import Testing

/// TA1 (design §8.3): the View ▸ Appearance preference, its application-wide override and its absence
/// from `RecordingWorkspace`. Serialized: two of the cases touch the real application appearance.
@Suite(.serialized)
struct RecordingAppearanceTests {
    /// TA1: the choice round-trips through `UserDefaults(suiteName:)`; System removes the key, a second
    /// controller on the same suite reads the stored choice back, and unknown values read as System.
    @Test @MainActor func preferenceRoundTripsThroughAPrivateDefaultsSuite() throws {
        try Self.withPrivateSuite { defaults in
            #expect(RecordingAppearancePreference.stored(in: defaults) == .system)
            var applied: [NSAppearance.Name?] = []
            let controller = RecordingAppearanceController(defaults: defaults) { applied.append($0?.name) }
            #expect(controller.preference == .system && applied == [nil])
            controller.preference = .dark
            #expect(defaults.string(forKey: RecordingAppearancePreference.defaultsKey) == "dark")
            #expect(RecordingAppearancePreference.stored(in: defaults) == .dark)
            let relaunched = RecordingAppearanceController(defaults: defaults) { applied.append($0?.name) }
            #expect(relaunched.preference == .dark && applied == [nil, .darkAqua, .darkAqua])
            controller.preference = .light
            #expect(RecordingAppearancePreference.stored(in: defaults) == .light && applied == [nil, .darkAqua, .darkAqua, .aqua])
            controller.preference = .light
            #expect(applied.count == 4, "an unchanged choice is neither stored nor applied again")
            controller.preference = .system
            #expect(defaults.object(forKey: RecordingAppearancePreference.defaultsKey) == nil)
            #expect(applied.count == 5 && applied[4] == nil)
            defaults.set("sepia", forKey: RecordingAppearancePreference.defaultsKey)
            #expect(RecordingAppearancePreference.stored(in: defaults) == .system)
            #expect(RecordingAppearancePreference.allCases.map(\.title) == ["System", "Light", "Dark"])
        }
    }

    /// TA1: applied to the real application, Light and Dark change the effective appearance of the app, a
    /// materialized window and the workspace it hosts at once, and System (`nil`) restores whatever was
    /// effective before, without a relaunch. The last override is the opposite of the system appearance,
    /// so the return to System is a real transition on a light and on a dark host alike.
    @Test @MainActor func systemRestoresTheEffectiveAppearanceWithoutRelaunch() throws {
        let application = NSApplication.shared
        application.setActivationPolicy(.prohibited)
        #expect(application.appearance == nil, "no earlier case may leave an override behind")
        defer { application.appearance = nil }
        let initial = application.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua])
        let overrides: [(RecordingAppearancePreference, NSAppearance.Name)] = initial == .darkAqua ? [(.dark, .darkAqua), (.light, .aqua)] : [(.light, .aqua), (.dark, .darkAqua)]
        try Self.withPrivateSuite { defaults in
            let controller = RecordingAppearanceController(defaults: defaults)
            let (window, host) = Self.host(RecordingWorkspace(state: .init()), appearance: nil)
            defer { window.close() }
            for (choice, expected) in overrides {
                controller.preference = choice
                #expect(application.appearance?.name == expected)
                #expect(application.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == expected)
                #expect(window.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == expected)
                let luminances = Self.cornerLuminances(host)
                #expect(luminances.allSatisfy { expected == .darkAqua ? $0 < 0.5 : $0 > 0.5 }, "\(choice): \(luminances)")
            }
            controller.preference = .system
            #expect(application.appearance == nil)
            #expect(application.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == initial)
            #expect(window.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == initial)
            let luminances = Self.cornerLuminances(host)
            #expect(luminances.allSatisfy { initial == .darkAqua ? $0 < 0.5 : $0 > 0.5 }, "system: \(luminances)")
        }
    }

    /// TA1: with Light stored in the defaults this process reads, a workspace hosted under a forced dark
    /// appearance still renders dark (and a forced light one light), and hosting it touches neither the
    /// window's nor the application's appearance: the override belongs to the scene, never to
    /// `RecordingWorkspace`. Both harness styles are hosted through both initializers: the shell harness
    /// forces the view appearance and the colour-scheme environment on a fixture workspace; the
    /// accessibility harness forces only the view appearance on a model-backed one, where a stray
    /// `preferredColorScheme` would set the window appearance and draw the content light.
    @Test @MainActor func hostedWorkspaceKeepsItsForcedAppearanceWhenLightIsStored() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        #expect(NSApplication.shared.appearance == nil)
        defer { NSApplication.shared.appearance = nil }
        // Registered defaults are what `.standard` reads in this process; nothing is written to disk.
        let registration = UserDefaults.standard.volatileDomain(forName: UserDefaults.registrationDomain)
        UserDefaults.standard.register(defaults: [RecordingAppearancePreference.defaultsKey: RecordingAppearancePreference.light.rawValue])
        defer { UserDefaults.standard.setVolatileDomain(registration, forName: UserDefaults.registrationDomain) }
        #expect(RecordingAppearancePreference.stored(in: .standard) == .light)
        for (name, dark) in [(NSAppearance.Name.darkAqua, true), (.aqua, false)] {
            func hosted<V: View>(_ view: V) -> (NSWindow, NSView) {
                let (window, host) = Self.host(view, appearance: NSAppearance(named: name)); return (window, host)
            }
            let shellStyle = hosted(RecordingWorkspace(state: .init()).environment(\.colorScheme, dark ? .dark : .light))
            let accessibilityStyle = hosted(RecordingWorkspace(model: RecordingWorkspaceModel(fixture: .init())))
            for (style, (window, host)) in [("shell", shellStyle), ("accessibility", accessibilityStyle)] {
                defer { window.close() }
                #expect(host.appearance?.name == name && host.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == name, "\(style)")
                let luminances = Self.cornerLuminances(host)
                #expect(luminances.count == 2 && luminances.allSatisfy { dark ? $0 < 0.5 : $0 > 0.5 }, "\(style) \(name.rawValue): \(luminances)")
                #expect(window.appearance == nil, "\(style): hosting the workspace must not set the window appearance")
                #expect(NSApplication.shared.appearance == nil, "\(style): hosting the workspace must not apply the stored preference")
            }
        }
    }

    /// A private defaults suite that is removed afterwards; `.standard` is never written. The suite is named by
    /// an absolute path inside a private temporary directory, so cfprefsd keeps it there and never leaves a
    /// domain plist under ~/Library/Preferences (removing a named domain leaves an empty plist behind).
    private static func withPrivateSuite<R>(_ body: (UserDefaults) throws -> R) throws -> R {
        let root = URL(fileURLWithPath: "/private/tmp/cole-native-appearance-defaults-" + UUID().uuidString.lowercased(), isDirectory: true)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: root) }
        let name = root.appendingPathComponent("defaults").path
        let defaults = try #require(UserDefaults(suiteName: name))
        defer { defaults.removePersistentDomain(forName: name); defaults.removeSuite(named: name) }
        return try body(defaults)
    }

    /// Hosts a view in a window that is never ordered; `appearance` is forced on the hosting view as the harnesses do.
    /// The window device is created at once (`defer: false`): only a materialized window follows the application
    /// appearance, as the scene's visible window does; a deferred one keeps the appearance it was created under.
    @MainActor private static func host<V: View>(_ view: V, appearance: NSAppearance?) -> (NSWindow, NSHostingView<V>) {
        let size = CGSize(width: 1060, height: 700)
        let host = NSHostingView(rootView: view)
        let window = NSWindow(contentRect: CGRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host; host.frame = CGRect(origin: .zero, size: size)
        host.appearance = appearance; host.layoutSubtreeIfNeeded()
        return (window, host)
    }

    /// Luminance of the sidebar corner and the opposite window corner of a fresh offscreen render.
    @MainActor private static func cornerLuminances(_ host: NSView) -> [Double] {
        host.layoutSubtreeIfNeeded()
        let size = host.bounds.size
        guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width), pixelsHigh: Int(size.height), bitsPerSample: 8,
            samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { return [] }
        bitmap.size = size
        host.cacheDisplay(in: host.bounds, to: bitmap)
        return [(2, 2), (bitmap.pixelsWide - 3, bitmap.pixelsHigh - 3)].compactMap { RecordingPalette.renderedLuminance(bitmap, x: $0, y: $1) }
    }
}
