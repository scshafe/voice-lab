import AppKit
import RecordingCore
import SwiftUI
import UniformTypeIdentifiers

/// The Speak page: type text, choose a model and a reference clip, hear the result, save a copy.
/// It uses no region probes (the Record page's geometry checks never see it), and its only key
/// command is ⌘↩ on Speak; the Record keyboard monitor is not mounted here.
@MainActor
public struct RecordingSpeakView: View {
    public static let placeholder = "Type what the voice should say"
    public static let idleStatus = "Nothing spoken yet this session"
    public static let emptyClips = "Clips you speak appear here"

    @ObservedObject private var model: RecordingWorkspaceModel
    @ObservedObject private var speak: RecordingSpeakModel
    @ObservedObject private var playback: RecordingPlaybackController
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.recordingReduceMotionOverride) private var reduceMotionOverride

    public init(model: RecordingWorkspaceModel) {
        self.model = model; speak = model.speak; playback = model.speak.playback
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Speak").font(.title2.weight(.semibold)).accessibilityAddTraits(.isHeader)
            HStack(alignment: .top, spacing: 20) {
                composer.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                listening.frame(width: 380).frame(maxHeight: .infinity, alignment: .topLeading)
            }
            status
        }
        .padding(RecordingLayout.inset)
        .frame(minWidth: RecordingLayout.minimumWidth, minHeight: RecordingLayout.minimumHeight, alignment: .topLeading)
        .background(Color(nsColor: .windowBackgroundColor))
        .transaction { if reduceMotionOverride ?? reduceMotion { $0.animation = nil } }
        .sheet(isPresented: Binding(get: { model.reviewConfirmation != nil }, set: { if !$0 { model.cancelReviewConfirmation() } })) {
            RecordingReviewConfirmationView(model: model)
        }
    }

    private var reducedMotion: Bool { reduceMotionOverride ?? reduceMotion }

    // MARK: composing

    private var composer: some View {
        VStack(alignment: .leading, spacing: 10) {
            ZStack(alignment: .topLeading) {
                TextEditor(text: $speak.text)
                    .font(.system(size: 17))
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .accessibilityLabel("Text to speak")
                    .accessibilityIdentifier("speak.text")
                if speak.text.isEmpty {
                    Text(Self.placeholder)
                        .font(.system(size: 17))
                        .foregroundStyle(RecordingPalette.supportingText)
                        .padding(.horizontal, 11).padding(.vertical, 6)
                        .allowsHitTesting(false)
                        .accessibilityIdentifier("speak.placeholder")
                }
            }
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color(nsColor: .separatorColor)))
            .frame(maxHeight: .infinity)

            HStack(spacing: 6) {
                let over = speak.textCount > RecordingSpeakModel.textLimit
                if over { Image(systemName: "exclamationmark.circle").foregroundStyle(.orange).accessibilityHidden(true) }
                Text(Self.countText(speak.textCount))
                    .font(.system(size: RecordingLayout.supportingTextSize).monospacedDigit())
                    .foregroundStyle(over ? Color.primary : RecordingPalette.supportingText)
                    .accessibilityLabel(Self.spokenCount(speak.textCount))
                    .accessibilityIdentifier("speak.count")
                Spacer()
            }

            HStack(spacing: 12) {
                Picker("Model", selection: $speak.selectedModelID) {
                    if speak.modelChoices.isEmpty {
                        Text("No fine-tuned model yet").tag(RecordingSpeakModelChoice.latestID)
                    }
                    ForEach(speak.modelChoices) { choice in Text(verbatim: choice.title).tag(choice.id) }
                }
                .pickerStyle(.menu).frame(maxWidth: 320, alignment: .leading)
                .disabled(speak.modelChoices.isEmpty || speak.isGenerating)
                .help("The fine-tuned model that speaks; Base is the voice model before fine-tuning")
                .accessibilityLabel("Model")
                .accessibilityIdentifier("speak.model")
                Picker("Reference", selection: $speak.selectedReferencePromptID) {
                    if speak.referenceChoices.isEmpty {
                        Text("Chosen by the service").tag(String?.none)
                    }
                    ForEach(speak.referenceChoices) { choice in Text(verbatim: choice.title).tag(Optional(choice.id)) }
                }
                .pickerStyle(.menu).frame(maxWidth: 320, alignment: .leading)
                .disabled(speak.referenceChoices.isEmpty || speak.isGenerating)
                .help("The recorded clip whose delivery the voice follows")
                .accessibilityLabel("Reference clip")
                .accessibilityIdentifier("speak.reference")
                Spacer(minLength: 0)
            }

            HStack(spacing: 12) {
                Button { speak.speak() } label: {
                    HStack(spacing: 6) {
                        if speak.isGenerating {
                            if reducedMotion { Image(systemName: "hourglass") } else { ProgressView().controlSize(.small) }
                        }
                        Text(speak.isGenerating ? "Generating…" : "Speak")
                    }.frame(minWidth: 110)
                }
                .buttonStyle(.borderedProminent).controlSize(.large)
                .keyboardShortcut(.return, modifiers: .command)
                .disabled(!speak.canSpeak)
                .help(speak.unavailableReason ?? "Speak the text with the chosen model (⌘↩)")
                .accessibilityLabel(speak.isGenerating ? "Generating speech" : "Speak")
                .accessibilityIdentifier("speak.generate")
                Button("Cancel") { speak.cancel() }
                    .controlSize(.large)
                    .disabled(!speak.canCancel)
                    .help(speak.cancelUnavailableReason ?? "Stop generating this speech")
                    .accessibilityLabel("Cancel speech generation")
                    .accessibilityIdentifier("speak.cancel")
                Text(speak.isGenerating ? "" : speak.unavailableReason ?? "")
                    .font(.system(size: RecordingLayout.supportingTextSize))
                    .foregroundStyle(RecordingPalette.supportingText)
                    .lineLimit(2)
                    .accessibilityIdentifier("speak.reason")
                Spacer(minLength: 0)
            }
        }
    }

    // MARK: listening

    private var listening: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Listen").font(.headline).accessibilityAddTraits(.isHeader)
            RecordingSpeakPlayer(speak: speak, playback: playback)
            HStack(spacing: 10) {
                Button("Save As…") { saveAs() }
                    .disabled(!speak.canSave)
                    .help(speak.saveUnavailableReason ?? "Save a copy of the selected clip as a WAV file")
                    .accessibilityLabel("Save the selected clip as")
                    .accessibilityIdentifier("speak.save-as")
                if speak.savingClipID != nil {
                    Text("Saving…").font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
                } else if let notice = speak.saveNotice {
                    if speak.saveFailed { Image(systemName: "exclamationmark.circle").foregroundStyle(.orange).accessibilityHidden(true) }
                    Text(verbatim: notice).font(.system(size: RecordingLayout.supportingTextSize))
                        .foregroundStyle(speak.saveFailed ? Color.primary : RecordingPalette.supportingText)
                        .lineLimit(2).help(notice)
                        .accessibilityLabel(speak.saveFailed ? "Warning: " + notice : notice)
                        .accessibilityIdentifier("speak.save-notice")
                }
                Spacer(minLength: 0)
            }
            Text("This session").font(.headline).padding(.top, 6).accessibilityAddTraits(.isHeader)
            clipList
        }
    }

    private var clipList: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 4) {
                if speak.clips.isEmpty {
                    Text(Self.emptyClips).font(.system(size: RecordingLayout.supportingTextSize))
                        .foregroundStyle(RecordingPalette.supportingText).padding(8)
                }
                ForEach(speak.clips) { clip in clipRow(clip) }
            }
        }
        .frame(maxHeight: .infinity)
        .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Clips spoken this session")
        .accessibilityIdentifier("speak.clips")
    }

    private func clipRow(_ clip: RecordingSpeakClip) -> some View {
        let selected = speak.selectedClipID == clip.id
        return Button { speak.selectClip(clip.id) } label: {
            VStack(alignment: .leading, spacing: 3) {
                Text(verbatim: clip.text).font(.system(size: 13, weight: selected ? .semibold : .regular)).lineLimit(2)
                Text(verbatim: clip.modelTitle + " · " + clip.durationText)
                    .font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
            }
            .padding(8).frame(maxWidth: .infinity, alignment: .leading)
            .background(selected ? Color.accentColor.opacity(0.14) : .clear, in: RoundedRectangle(cornerRadius: 6))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(clip.text + ", " + clip.modelTitle + ", " + clip.durationText)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("speak.clip." + clip.id)
    }

    // MARK: status

    private var status: some View {
        let line = speak.statusLine ?? Self.idleStatus
        return HStack(spacing: 6) {
            if speak.needsAttention { Image(systemName: "exclamationmark.circle").foregroundStyle(.orange).accessibilityHidden(true) }
            Text(verbatim: line).lineLimit(2)
                .accessibilityLabel(speak.needsAttention ? "Warning: " + line : line)
                .accessibilityAddTraits(.updatesFrequently)
            Spacer(minLength: 0)
        }
        .font(.system(size: RecordingLayout.supportingTextSize))
        .foregroundStyle(RecordingPalette.supportingText)
        .frame(maxWidth: .infinity, minHeight: 20, alignment: .leading)
        .help(line)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Speak status")
        .accessibilityIdentifier("speak.status")
    }

    // MARK: Save As

    private func saveAs() {
        guard speak.canSave, let clip = speak.selectedClip else { return }
        let panel = NSSavePanel()
        panel.title = "Save Speech"
        panel.allowedContentTypes = [.wav]
        panel.nameFieldStringValue = RecordingSpeakModel.suggestedFileName(for: clip)
        panel.canCreateDirectories = true
        panel.isExtensionHidden = false
        let speak = speak
        let finish: @MainActor (NSApplication.ModalResponse) -> Void = { response in
            guard response == .OK, let destination = panel.url else { return }
            speak.saveClip(clip.id, to: destination)
        }
        if let window = NSApp.keyWindow {
            panel.beginSheetModal(for: window) { response in MainActor.assumeIsolated { finish(response) } }
        } else {
            finish(panel.runModal())
        }
    }

    // MARK: text

    public nonisolated static func countText(_ count: Int) -> String {
        count.formatted() + " / " + RecordingSpeakModel.textLimit.formatted()
    }

    public nonisolated static func spokenCount(_ count: Int) -> String {
        "\(count.formatted()) of \(RecordingSpeakModel.textLimit.formatted()) characters"
    }
}

/// Play/pause, the position (scrubbed) and elapsed/total for the selected clip. The position
/// is polled from the player clock only while it plays.
@MainActor
struct RecordingSpeakPlayer: View {
    @ObservedObject var speak: RecordingSpeakModel
    @ObservedObject var playback: RecordingPlaybackController
    @State private var scrubFrame: Double?
    @State private var dragging = false

    var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 30, paused: playback.phase != .playing)) { timeline in
            content.task(id: timeline.date) { await playback.refreshPosition() }
        }
    }

    private var frames: Int64 { speak.selectedClip?.frames ?? 0 }
    private var loaded: Bool { playback.source?.takeID == speak.selectedClipID && speak.selectedClipID != nil }
    private var positionFrame: Int64 { scrubFrame.map { Int64($0) } ?? (loaded ? playback.cursorFrame : 0) }

    private var content: some View {
        let playing = playback.phase == .playing
        let time = RecordingSpeakModel.timeText(frames: positionFrame) + " / " + RecordingSpeakModel.timeText(frames: frames)
        return VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Button { speak.playPause() } label: {
                    Image(systemName: playing ? "pause.fill" : "play.fill").frame(width: 20)
                }
                .disabled(!speak.canPlay)
                .help(speak.playUnavailableReason ?? (playing ? "Pause" : "Play the selected clip"))
                .accessibilityLabel(playing ? "Pause speech" : "Play speech")
                .accessibilityIdentifier("speak.play-pause")
                Slider(value: Binding(get: { scrubFrame ?? Double(positionFrame) }, set: { value in
                    if dragging { scrubFrame = value } else { speak.seek(to: Int64(value)) }
                }), in: 0...Double(max(1, frames)), onEditingChanged: { editing in
                    dragging = editing
                    if !editing, let target = scrubFrame { speak.seek(to: Int64(target)); scrubFrame = nil }
                })
                .disabled(!speak.canPlay)
                .accessibilityLabel("Playback position")
                .accessibilityValue(time)
                .accessibilityIdentifier("speak.position")
                Text(time).font(.system(size: 13, design: .monospaced)).monospacedDigit()
                    .accessibilityLabel("Elapsed and total time")
                    .accessibilityValue(time)
                    .accessibilityIdentifier("speak.time")
            }
            if let reason = playbackReason {
                HStack(spacing: 8) {
                    Text(verbatim: reason).font(.system(size: RecordingLayout.supportingTextSize))
                        .foregroundStyle(playback.failure != nil ? Color.primary : RecordingPalette.supportingText)
                        .lineLimit(2).help(reason)
                        .accessibilityIdentifier("speak.playback-notice")
                    Spacer(minLength: 0)
                    if playback.failure != nil || speak.clipFailure != nil {
                        Button("Recover audio") { speak.recoverPlayback() }
                            .disabled(playback.captureOwned)
                            .help("Reset the Speak audio output and load the selected clip again")
                            .accessibilityIdentifier("speak.recover")
                    }
                }
            }
        }
    }

    /// A failure or refusal worth showing under the player; nothing while all is well.
    private var playbackReason: String? {
        if let failure = playback.failure { return failure.message }
        if let clipFailure = speak.clipFailure { return clipFailure }
        return speak.playbackNotice
    }
}
