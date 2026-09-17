import AppKit
import RecordingCore
import SwiftUI

/// Persistent native workspace. Geometry depends only on window size and explicit Focus choice.
@MainActor
public struct RecordingWorkspace: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    public init(model: RecordingWorkspaceModel) { self.model = model }
    public init(state: RecordingShellState = RecordingShellState()) { model = RecordingWorkspaceModel(fixture: state) }

    public var body: some View {
        GeometryReader { geometry in
            let regions = RecordingLayout.regions(size: geometry.size, focusMode: model.focusMode)
            ZStack(alignment: .topLeading) {
                Color(nsColor: .windowBackgroundColor)
                if let frame = regions["library"] { library.region("library", frame: frame) }
                header.region("header", frame: regions["header"]!)
                RecordingScriptCard(prompt: model.state.prompt, scriptSize: model.scriptSize).region("script", frame: regions["script"]!)
                RecordingTakePicker(model: model).region("take_picker", frame: regions["take_picker"]!)
                RecordingReviewWaveform(model: model)
                    .clipShape(RoundedRectangle(cornerRadius: 8)).region("waveform", frame: regions["waveform"]!)
                RecordingTrimControls(model: model).region("trim", frame: regions["trim"]!)
                RecordingMediaTimer(model: model).region("timer", frame: regions["timer"]!)
                RecordingPlaybackControls(model: model).region("playback", frame: regions["playback"]!)
                RecordingTransportControls(model: model).region("footer", frame: regions["footer"]!)
                RecordingReviewStatus(model: model).region("status", frame: regions["status"]!)
                    .sheet(isPresented: $model.recoverySheetOpen) { RecordingRecoveryView(model: model) }
            }
        }
        .frame(minWidth: RecordingLayout.minimumWidth, minHeight: RecordingLayout.minimumHeight)
        .background(RecordingWorkspaceKeyboard(model: model))
        .sheet(isPresented: Binding(get: { model.reviewConfirmation != nil }, set: { if !$0 { model.cancelReviewConfirmation() } })) {
            RecordingReviewConfirmationView(model: model)
        }
    }

    private var library: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Library").font(.title2.weight(.semibold)); Spacer()
                // Prompts with a kept take over the active catalog's prompts.
                let coverage = model.libraryCoverage
                Text(coverage.text).font(.system(size: RecordingLayout.supportingTextSize).monospacedDigit()).foregroundStyle(RecordingPalette.supportingText)
                    .accessibilityLabel(coverage.spokenText).accessibilityIdentifier("library.coverage")
            }
            // The primary library action sits above search and the filters, which never affect it (§6.4).
            // It is a plain button, so the search field stays the sidebar's first editable field.
            Button { model.continueMissing() } label: { Text("Continue Missing").frame(maxWidth: .infinity) }
                .buttonStyle(.borderedProminent).controlSize(.large).disabled(!model.canContinueMissing)
                .help(model.state.historyCompleteness == "complete" ? "Selects the next missing prompt in catalog order; ignores search and filters." : "Waiting for complete library metadata")
                .accessibilityIdentifier("library.continueMissing")
            TextField("Search prompts", text: $model.searchText)
                .textFieldStyle(.roundedBorder).accessibilityLabel("Search library prompts").accessibilityIdentifier("library.search")
            Picker("Show", selection: $model.libraryFilter) {
                Text("All prompts").tag("all")
                Text("Missing").tag("missing")
                Text("Needs attention").tag("attention")
            }.pickerStyle(.menu).accessibilityIdentifier("library.filter")
            // Label toggles combine with OR; catalog-derived titles render verbatim, never as Markdown.
            Menu {
                ForEach(model.labelOptions, id: \.self) { token in
                    Toggle(isOn: Binding(get: { model.selectedLabels.contains(token) }, set: { model.setLabel(token, selected: $0) })) {
                        Text(verbatim: RecordingPromptPresentation.labelTitle(token))
                    }
                }
                Divider()
                Button("Show all labels") { model.showAllLabels() }
            } label: { Text(verbatim: model.labelsMenuTitle) }
                .accessibilityIdentifier("library.labels")
            Divider()
            if model.currentPromptFilteredOut, let current = model.state.prompt {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Current prompt · outside filter").font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
                    promptRow(current)
                }
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 4) {
                    ForEach(model.filteredPrompts) { prompt in promptRow(prompt) }
                }
            }
            if model.state.promptCursor != nil {
                Button("Load more prompt status") { Task { await model.loadMorePromptStatuses() } }
                    .buttonStyle(.borderless).font(.system(size: RecordingLayout.supportingTextSize))
            }
            Text(model.state.historyCompleteness == "complete" ? (model.state.promptCursor == nil ? "Library up to date" : "More prompt status available") : "Library \(model.state.historyCompleteness)")
                .font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
        }
        .padding(16).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(nsColor: .controlBackgroundColor))
    }

    private func promptRow(_ prompt: RecordingPromptPresentation) -> some View {
        Button { model.selectPrompt(prompt.id) } label: {
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: prompt.librarySymbol).frame(width: 18)
                VStack(alignment: .leading, spacing: 3) {
                    Text(prompt.title).font(.system(size: 13, weight: model.state.prompt?.id == prompt.id ? .semibold : .regular)).lineLimit(2)
                    Text(statusTitle(model.state.promptStatuses[prompt.id] ?? "unknown")).font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
                }
                Spacer(minLength: 0)
                if model.state.prompt?.id == prompt.id { Image(systemName: "checkmark").font(.system(size: RecordingLayout.supportingTextSize)) }
            }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                .background(model.state.prompt?.id == prompt.id ? Color.accentColor.opacity(0.14) : .clear, in: RoundedRectangle(cornerRadius: 6))
        }.buttonStyle(.plain).disabled(!model.state.canNavigate && model.state.prompt?.id != prompt.id)
            .accessibilityLabel(prompt.title + ", " + statusTitle(model.state.promptStatuses[prompt.id] ?? "unknown"))
            .accessibilityIdentifier("prompt." + prompt.id)
    }

    private var header: some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(model.state.prompt?.title ?? "Recording").font(.headline).lineLimit(1)
                if let prompt = model.state.prompt { RecordingLabelChips(prompt: prompt) }
                else { Text(model.promptPlaceholder).font(.system(size: 13)).foregroundStyle(RecordingPalette.supportingText) }
            }.frame(maxWidth: .infinity, alignment: .leading)
            Menu {
                Picker("Reading text size", selection: $model.scriptSize) {
                    Text("Standard · 24 pt").tag(CGFloat(24)); Text("Large · 28 pt").tag(CGFloat(28)); Text("Extra large · 32 pt").tag(CGFloat(32))
                }
            } label: { Image(systemName: "textformat.size") }.menuStyle(.borderlessButton).frame(width: 28)
                .accessibilityLabel("Reading text size")
            Button { model.focusMode.toggle() } label: { Image(systemName: model.focusMode ? "sidebar.left" : "sidebar.left") }
                .buttonStyle(.borderless).accessibilityLabel(model.focusMode ? "Show library" : "Enter Focus mode")
                .help(model.focusMode ? "Show library" : "Hide library for Focus mode")
            Label(model.state.deviceReady ? "UMIK-1" : "Microphone", systemImage: model.state.deviceReady ? "mic" : "mic.slash")
                .font(.system(size: 13)).foregroundStyle(model.state.deviceReady ? Color.primary : RecordingPalette.supportingText)
                .help(model.state.deviceReason)
        }
    }

    private func statusTitle(_ status: String) -> String {
        switch status {
        case "saved-checking", "pending", "running": "Saved · checking"
        case "needs-review": "Needs review"
        case "verified", "recorded": "Recorded"
        case "draft": "Draft"
        case "missing": "Missing"
        case "invalid": "Invalid take"
        case "failed": "Check failed"
        default: "Unknown"
        }
    }
}

private extension View {
    func region(_ name: String, frame: CGRect) -> some View {
        self.frame(width: frame.width, height: frame.height)
            .background(RecordingRegionProbe(regionName: name))
            .position(x: frame.midX, y: frame.midY)
    }
}
