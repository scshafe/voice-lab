import RecordingCore
import SwiftUI

/// The one-line status region. Its words and warning come from the model's `statusRegion`;
/// the review and playback objects are observed so their changes redraw it.
@MainActor
struct RecordingReviewStatus: View {
    @ObservedObject var model: RecordingWorkspaceModel
    @ObservedObject private var review: RecordingReviewController
    @ObservedObject private var playback: RecordingPlaybackController
    init(model: RecordingWorkspaceModel) { self.model = model; review = model.review; playback = model.review.playback }
    var body: some View {
        let status = model.statusRegion
        HStack(spacing: 6) {
            if status.needsAttention {
                // The spoken line says "Warning" itself; the glyph is decoration for VoiceOver.
                Image(systemName: "exclamationmark.circle").foregroundStyle(.orange).accessibilityHidden(true)
            }
            Text(status.line).lineLimit(1)
                .accessibilityLabel(status.spokenLine)
                .accessibilityAddTraits(.updatesFrequently)
            Spacer()
            if model.canOpenRecovery {
                Button("Recovery…") { model.openRecoverySheet() }.buttonStyle(.borderless)
                    .help("Review preserved recordings that need attention; originals are retained")
                    .accessibilityIdentifier("recording.open-recovery")
            }
            if model.audioReconnectRequired {
                Button("Reconnect audio") { Task { await model.reconnectAudio() } }
                    .buttonStyle(.borderless).disabled(!model.canReconnectAudio)
                    .help(model.canReconnectAudio ? "Check the microphone and continue recording" : "Finish the current take and any unsaved trim before reconnecting audio")
                    .accessibilityIdentifier("recording.reconnect-audio")
            } else if model.state.phase == .ready && !model.state.canRecord && !model.state.unsealedRecheckPending {
                Button("Check again") { Task { await model.refreshReadiness() } }.buttonStyle(.borderless)
            }
        }.font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
            .frame(maxWidth: .infinity, alignment: .leading).help(status.line)
            .accessibilityElement(children: .contain).accessibilityLabel("Status")
            .accessibilityIdentifier("recording.status")
    }
}
