import AppKit
import SwiftUI

/// The View ▸ Appearance choice, remembered as a user preference. System means no override.
public enum RecordingAppearancePreference: String, CaseIterable, Identifiable, Sendable {
    case system, light, dark

    /// The key in the application's defaults domain; an absent or unknown value reads as System.
    public static let defaultsKey = "RecordingAppearance"
    public var id: String { rawValue }
    public var title: String {
        switch self {
        case .system: "System"
        case .light: "Light"
        case .dark: "Dark"
        }
    }
    /// The application-wide appearance for this choice; `nil` lets the system appearance through.
    public var nsAppearance: NSAppearance? {
        switch self {
        case .system: nil
        case .light: NSAppearance(named: .aqua)
        case .dark: NSAppearance(named: .darkAqua)
        }
    }

    public static func stored(in defaults: UserDefaults) -> Self {
        defaults.string(forKey: defaultsKey).flatMap(Self.init(rawValue:)) ?? .system
    }
    /// System leaves no key behind, so a fresh domain and the default choice are the same state.
    public func store(in defaults: UserDefaults) {
        if self == .system { defaults.removeObject(forKey: Self.defaultsKey) } else { defaults.set(rawValue, forKey: Self.defaultsKey) }
    }
}

/// Owns the stored choice and applies it to the whole application. `NSApp.appearance` reaches SwiftUI
/// content, AppKit sheets, popovers and menus and `RecordingPalette`'s dynamic colour alike, and setting
/// it back to `nil` restores the system appearance at once; `.preferredColorScheme(nil)` does not
/// reliably revert on macOS and would not reach the AppKit surfaces. Only the application scene creates
/// one: `RecordingWorkspace` never reads the preference, so harness-hosted workspaces keep the appearance
/// forced on their hosting view.
@MainActor
public final class RecordingAppearanceController: ObservableObject {
    @Published public var preference: RecordingAppearancePreference {
        didSet { guard preference != oldValue else { return }; preference.store(in: defaults); apply() }
    }
    private let defaults: UserDefaults
    private let applier: @MainActor (NSAppearance?) -> Void

    /// Reads and applies the stored choice at once. Tests inject a private suite and a recording applier.
    public init(defaults: UserDefaults, applier: @escaping @MainActor (NSAppearance?) -> Void = { NSApplication.shared.appearance = $0 }) {
        self.defaults = defaults; self.applier = applier
        preference = RecordingAppearancePreference.stored(in: defaults)
        apply()
    }

    /// Applies the current choice; System passes `nil`, which restores the system appearance.
    public func apply() { applier(preference.nsAppearance) }
}
