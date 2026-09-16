import SwiftUI
import RecordingCore

@MainActor
public struct RecordingTrimControls: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    @ObservedObject private var review: RecordingReviewController
    public init(model: RecordingWorkspaceModel) { self.model = model; review = model.review }
    public var body: some View {
        HStack(spacing: 8) {
            Text("In")
            TextField("In frame", text: Binding(get: { review.startText }, set: { review.setStartText($0) }))
                .frame(width: 100).accessibilityLabel("Trim in frame").accessibilityIdentifier("trim.start")
            Text("Out")
            TextField("Out frame", text: Binding(get: { review.endText }, set: { review.setEndText($0) }))
                .frame(width: 100).accessibilityLabel("Trim out frame").accessibilityIdentifier("trim.end")
            Button("Save trim") { model.saveTrim() }.disabled(!model.canEditTrim || !review.isDirty)
                .accessibilityIdentifier("trim.save")
            Button("Reset") { model.resetTrim() }.disabled(!model.canEditTrim)
                .help("Restore the whole original frame range; original PCM stays intact").accessibilityIdentifier("trim.reset")
            Spacer(minLength: 0)
            Button { zoom(-1) } label: { Image(systemName: "minus.magnifyingglass") }.accessibilityLabel("Zoom waveform out")
            Button { zoom(1) } label: { Image(systemName: "plus.magnifyingglass") }.accessibilityLabel("Zoom waveform in")
            Toggle("Fit", isOn: $review.fitDisplay).toggleStyle(.button)
                .help("Fit display amplitude only; original audio and measurements stay unchanged")
                .accessibilityLabel("Fit waveform display amplitude")
        }.font(.system(size: 13, design: .monospaced)).controlSize(.small)
            .textFieldStyle(.roundedBorder)
            .disabled(!model.canSelectTake || model.selectedReview == nil || review.loading)
            .help(review.fieldError ?? "Trim uses integer source frames at 48,000 frames per second")
    }
    private func zoom(_ factor: Int) {
        do { try review.zoom(factor, waveform: model.waveform) } catch { review.report(error) }
    }
}

@MainActor
public struct RecordingPlaybackControls: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    @ObservedObject private var review: RecordingReviewController
    @ObservedObject private var playback: RecordingPlaybackController
    public init(model: RecordingWorkspaceModel) { self.model = model; review = model.review; playback = model.review.playback }
    public var body: some View {
        HStack(spacing: 22) {
            Button { model.seekSelected(to: review.trimRange.lowerBound) } label: { Image(systemName: "backward.end.fill") }
                .accessibilityLabel("Seek to retained start").accessibilityIdentifier("playback.start")
            Button { model.skipSelected(frames: -4_800) } label: { Image(systemName: "gobackward") }
                .accessibilityLabel("Seek backward 0.1 seconds").accessibilityIdentifier("playback.backward")
            Button { model.playPauseSelected() } label: { Image(systemName: playback.phase == .playing ? "pause.fill" : "play.fill") }
                .accessibilityLabel(playback.phase == .playing ? "Pause selected take" : "Play selected take")
                .accessibilityIdentifier("playback.play-pause")
            Button { model.skipSelected(frames: 4_800) } label: { Image(systemName: "goforward") }
                .accessibilityLabel("Seek forward 0.1 seconds").accessibilityIdentifier("playback.forward")
            Button { model.seekSelected(to: review.trimRange.upperBound) } label: { Image(systemName: "forward.end.fill") }
                .accessibilityLabel("Seek to retained end").accessibilityIdentifier("playback.end")
        }.disabled(!model.canPlaySelected)
            .overlay(alignment: .trailing) {
                Button("Recover audio") { model.recoverPlayback() }
                    .opacity(playback.failure != nil || review.failure != nil ? 1 : 0)
                    .disabled(model.state.showsStop || (playback.failure == nil && review.failure == nil))
                    .offset(x: 150).accessibilityHidden(playback.failure == nil && review.failure == nil)
            }
    }
}

@MainActor
public struct RecordingReviewWaveform: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    @ObservedObject private var review: RecordingReviewController
    @ObservedObject private var playback: RecordingPlaybackController
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.recordingReduceMotionOverride) private var reduceMotionOverride
    public init(model: RecordingWorkspaceModel) { self.model = model; review = model.review; playback = model.review.playback }
    public var body: some View {
        TimelineView(.animation(minimumInterval: 1 / 60, paused: playback.phase != .playing)) { timeline in
            RecordingWaveformView(model: model.waveform, isRecording: model.state.showsStop,
                viewport: model.state.showsStop ? nil : review.viewport, fitting: review.fitDisplay,
                positionFrame: model.selectedReview == nil ? nil : playback.cursorFrame,
                trimRange: model.selectedReview == nil ? nil : review.trimRange,
                onSeek: { model.seekSelected(to: $0) }, onTrim: { range in if model.canEditTrim { review.preview(range) } })
                .task(id: timeline.date) { await playback.refreshPosition() }
        }
        .transaction { if reduceMotionOverride ?? reduceMotion { $0.animation = nil } }
        .background(RecordingWaveformKeyboardFocus())
    }
}

/// Explicit render-fixture override; production follows the macOS accessibility preference.
private struct RecordingReduceMotionOverrideKey: EnvironmentKey { static let defaultValue: Bool? = nil }
extension EnvironmentValues {
    public var recordingReduceMotionOverride: Bool? {
        get { self[RecordingReduceMotionOverrideKey.self] }
        set { self[RecordingReduceMotionOverrideKey.self] = newValue }
    }
}
