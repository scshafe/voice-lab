import SwiftUI

@MainActor
public struct RecordingReviewConfirmationView: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    public init(model: RecordingWorkspaceModel) { self.model = model }
    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            switch model.reviewConfirmation {
            case .discardTrim:
                Text("Save changes to the trim?").font(.headline)
                Text("Your original recording is retained. Choose what to do with the unsaved frame range.")
                HStack {
                    Button("Cancel") { model.cancelReviewConfirmation() }.keyboardShortcut(.cancelAction)
                    Spacer()
                    Button("Discard trim") { model.discardTrimAndContinue() }
                    Button("Save trim") { model.saveTrimAndContinue() }.disabled(!model.canEditTrim)
                }
            case .decision(let action):
                Text(action == "remove" ? "Remove this take from use?" : "Reject this take?").font(.headline)
                Text("The original recording stays available in history and can be restored.")
                TextField("Reason", text: $model.decisionReason).textFieldStyle(.roundedBorder)
                    .accessibilityLabel("Take decision reason")
                HStack {
                    Button("Cancel") { model.cancelReviewConfirmation() }.keyboardShortcut(.cancelAction)
                    Spacer()
                    Button(action == "remove" ? "Remove" : "Reject") { model.confirmDecision() }
                        .disabled(model.decisionReason.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.decisionReason.count > 160)
                }
            case .quitDuringFineTune:
                Text(RecordingWorkspaceModel.quitDuringFineTuneMessage).font(.headline)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("quit-fine-tune.message")
                HStack {
                    Button("Keep running") { model.cancelReviewConfirmation() }.keyboardShortcut(.cancelAction)
                        .accessibilityIdentifier("quit-fine-tune.keep-running")
                    Spacer()
                    Button("Quit and stop fine-tune", role: .destructive) { model.quitAndStopFineTune() }
                        .accessibilityIdentifier("quit-fine-tune.quit")
                }
            case nil: Text("Review decision complete")
            }
        }.padding(24).frame(width: 430).fixedSize(horizontal: false, vertical: true)
    }
}
