import RecordingCore
import SwiftUI

@MainActor
struct RecordingReviewStatus: View {
    @ObservedObject var model: RecordingWorkspaceModel
    @ObservedObject private var review: RecordingReviewController
    @ObservedObject private var playback: RecordingPlaybackController
    init(model: RecordingWorkspaceModel) { self.model = model; review = model.review; playback = model.review.playback }
    private var message: String {
        if model.state.showsStop { return model.state.status }
        if let error = playback.failure ?? review.failure { return error.message }
        if let error = review.fieldError { return error }
        if review.isDirty { return "Unsaved trim preview · original retained" }
        if review.cacheStatus == "needs-rebuild" { return "Waveform archive unavailable · displayed peaks ready; rebuild available" }
        // A capture terminal's own words are never masked by background recovery progress.
        if let notice = model.captureTerminalNotice, notice == model.state.status { return notice }
        return model.recoveryStatusLine ?? model.fineTuneStatusLine ?? model.legacyReviewStatus ?? model.state.status
    }
    var body: some View {
        HStack(spacing: 6) {
            if model.state.errorCode != nil || playback.failure != nil || review.failure != nil || review.fieldError != nil || model.fineTuneNeedsAttention || !model.recoveryAttentionRows.isEmpty {
                Image(systemName: "exclamationmark.circle").foregroundStyle(.orange)
            }
            Text(message).lineLimit(1)
            Spacer()
            if model.canOpenRecovery {
                Button("Recovery…") { model.openRecoverySheet() }.buttonStyle(.borderless)
                    .help("Review preserved recordings that need attention; originals are retained")
                    .accessibilityIdentifier("recording.open-recovery")
            }
            if model.audioReconnectRequired {
                Button("Reconnect audio") { Task { await model.reconnectAudio() } }
                    .buttonStyle(.borderless).disabled(!model.canReconnectAudio)
                    .help(model.canReconnectAudio ? "Check the microphone and create a resumed recording context" : "Finish the current take and any unsaved trim before reconnecting audio")
                    .accessibilityIdentifier("recording.reconnect-audio")
            } else if model.state.phase == .ready && !model.state.canRecord && !model.state.unsealedRecheckPending {
                Button("Check again") { Task { await model.refreshReadiness() } }.buttonStyle(.borderless)
            }
        }.font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
            .frame(maxWidth: .infinity, alignment: .leading).help(message).accessibilityIdentifier("recording.status")
    }
}
