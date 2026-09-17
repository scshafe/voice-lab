import SwiftUI
import RecordingCore

/// Persistent controls with dependency-specific availability; Stop stays red through sealing.
@MainActor
public struct RecordingTransportControls: View {
    /// Fits the wider of the two slot labels at the system font size.
    static let fineTuneLabelWidth: CGFloat = 104
    @ObservedObject private var model: RecordingWorkspaceModel
    @ObservedObject private var waveform: RecordingWaveformModel
    @ObservedObject private var playback: RecordingPlaybackController
    @ObservedObject private var review: RecordingReviewController
    public init(model: RecordingWorkspaceModel) { self.model = model; waveform = model.waveform; review = model.review; playback = model.review.playback }
    public var body: some View {
        HStack(spacing: 16) {
            Button {
                if model.state.canStop { model.stop() }
                else { model.record() }
            } label: {
                ZStack {
                    Circle().stroke(Color.red.opacity(0.8), lineWidth: 2)
                    if model.state.showsStop {
                        RoundedRectangle(cornerRadius: 4).fill(Color.red).frame(width: 24, height: 24)
                    } else {
                        Circle().fill(Color.red).padding(6)
                    }
                }.frame(width: RecordingLayout.recordTarget, height: RecordingLayout.recordTarget)
                    .contentShape(Circle())
            }
            .buttonStyle(.plain).disabled(!model.state.canStop && !model.canRecord)
            .accessibilityLabel(model.state.showsStop ? "Stop" : "Record")
            .accessibilityIdentifier("transport.record-stop")
            .help(model.state.canStop ? "Stop the current capture immediately" : model.recordUnavailableReason)
            Text(model.state.showsStop ? "Stop" : "Record").font(.system(size: 13, weight: .medium))
            Spacer()
            Menu {
                Button("Retake") { model.requestRetake() }.disabled(model.state.takeID == nil)
                Button("Remove…") { model.requestDecision("remove") }.disabled(model.selectedReview == nil)
                Button("Reject…") { model.requestDecision("reject") }.disabled(model.selectedReview == nil)
                Button("Restore") { model.requestDecision("restore") }.disabled(!["removed", "rejected"].contains(model.selectedDecision ?? ""))
            } label: { Text("Take actions") }.disabled(!model.canSelectTake || model.state.reservationUnsealed)
            Button("Retake") { model.requestRetake() }.disabled(!model.canSelectTake || model.state.takeID == nil || model.state.reservationUnsealed)
                .help("Retain this independent take and prepare another recording").accessibilityIdentifier("transport.retake")
            // One slot: Fine-tune model, or Cancel fine-tune while a job is queued or running.
            // Both labels share one width, so switching never moves Retake or Save.
            if model.fineTune.isActive {
                Button { model.cancelFineTune() } label: { Text("Cancel fine-tune").frame(width: Self.fineTuneLabelWidth) }
                    .disabled(!model.canCancelFineTune).help(model.fineTuneCancelReason)
                    .accessibilityLabel("Cancel fine-tune").accessibilityIdentifier("transport.cancel-fine-tune")
            } else {
                Button { model.startFineTune() } label: { Text("Fine-tune model").frame(width: Self.fineTuneLabelWidth) }
                    .disabled(!model.canStartFineTune).help(model.fineTuneUnavailableReason)
                    .accessibilityLabel("Fine-tune model").accessibilityIdentifier("transport.fine-tune")
            }
            Button { model.save() } label: { Text(model.saveActionTitle).frame(width: 80) }
                .disabled(!model.canSave)
                .help(model.saveUnavailableReason).accessibilityIdentifier("transport.save-next")
        }
    }
}
