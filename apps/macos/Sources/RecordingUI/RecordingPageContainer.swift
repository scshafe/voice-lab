import RecordingCore
import SwiftUI

/// The window's two pages. Record is the recording workspace; Speak types text and hears it in
/// the fine-tuned voice.
public enum RecordingPage: String, CaseIterable, Identifiable, Sendable {
    case record, speak

    public var id: String { rawValue }
    public var title: String {
        switch self {
        case .record: "Record"
        case .speak: "Speak"
        }
    }
    /// The page control's accessibility identifier.
    public var identifier: String { "page." + rawValue }
    /// The View menu item's title and its ⌘ key.
    public var menuTitle: String { title + " Page" }
    public var shortcut: KeyEquivalent {
        switch self {
        case .record: "1"
        case .speak: "2"
        }
    }
}

@MainActor
extension RecordingWorkspaceModel {
    public static let pageSwitchRecordingReason = "Finish the current take before switching pages"
    public static let pageSwitchSavingReason = "Wait for the take to finish saving before switching pages"
    public static let pageSwitchQuestionReason = "Answer the open question before switching pages"
    public static let pageSwitchHelp = "Switch between recording takes and speaking text (⌘1, ⌘2)"

    /// Why the page cannot change now: a take is being recorded or saved, or a confirmation
    /// sheet is open (it belongs to the page that asked).
    public var pageSwitchUnavailableReason: String? {
        if state.showsStop { return Self.pageSwitchRecordingReason }
        if state.phase == .saving { return Self.pageSwitchSavingReason }
        if reviewConfirmation != nil { return Self.pageSwitchQuestionReason }
        return nil
    }

    public var canSwitchPage: Bool { pageSwitchUnavailableReason == nil }

    /// Show `next`. The page being left pauses its playback, so the two transports never play
    /// at once; its other state (a trim preview, the Speak text and clips) stays as it was.
    public func showPage(_ next: RecordingPage) {
        guard next != page, canSwitchPage else { return }
        let leaving = page
        page = next
        pageSwitchTask = Task { [weak self] in await self?.pausePlayback(of: leaving) }
    }

    /// Returns once the page being left has paused its playback.
    public func pageSwitchSettled() async { await pageSwitchTask?.value }

    private func pausePlayback(of page: RecordingPage) async {
        switch page {
        case .record:
            do { try await review.playback.pause() } catch { review.report(error) }
        case .speak:
            do { try await speak.playback.pause() } catch { speak.reportPlayback(error) }
        }
    }
}

/// The window content: the Record workspace (its frame and regions unchanged) or the Speak
/// view, with the page control in the window toolbar. Only the shown page is mounted, so the
/// Record keyboard monitor exists only while Record is shown. Hosts with no window toolbar
/// (harnesses, tests) show the page alone.
@MainActor
public struct RecordingPageContainer: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    public init(model: RecordingWorkspaceModel) { self.model = model }

    public var body: some View {
        Group {
            switch model.page {
            case .record: RecordingWorkspace(model: model)
            case .speak: RecordingSpeakView(model: model)
            }
        }
        .toolbar {
            ToolbarItem(placement: .principal) { RecordingPagePicker(model: model) }
        }
    }
}

/// The segmented Record | Speak control.
@MainActor
public struct RecordingPagePicker: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    public init(model: RecordingWorkspaceModel) { self.model = model }

    public var body: some View {
        Picker("Page", selection: Binding(get: { model.page }, set: { model.showPage($0) })) {
            ForEach(RecordingPage.allCases) { page in
                Text(page.title).tag(page)
                    .accessibilityLabel(page.title + " page")
                    .accessibilityIdentifier(page.identifier)
            }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .fixedSize()
        .disabled(!model.canSwitchPage)
        .help(model.pageSwitchUnavailableReason ?? Self.help)
        .accessibilityIdentifier("page.picker")
    }

    static let help = RecordingWorkspaceModel.pageSwitchHelp
}

/// View ▸ Record Page (⌘1) and Speak Page (⌘2); the shown page is checked.
@MainActor
public struct RecordingPageMenuItems: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    public init(model: RecordingWorkspaceModel) { self.model = model }

    public var body: some View {
        ForEach(RecordingPage.allCases) { page in
            Toggle(page.menuTitle, isOn: Binding(get: { model.page == page }, set: { if $0 { model.showPage(page) } }))
                .keyboardShortcut(page.shortcut, modifiers: .command)
                .disabled(page != model.page && !model.canSwitchPage)
        }
    }
}
