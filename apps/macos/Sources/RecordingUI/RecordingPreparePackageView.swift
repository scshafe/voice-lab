import SwiftUI

/// The explicit Prepare for lab flow. It explains missing prerequisites in place and never
/// redirects Save & Next, inserts room tone into the recording queue, or claims that a
/// prepared package is accepted raw or authorization to transfer or synthesize anything.
@MainActor
public struct RecordingPreparePackageView: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    public init(model: RecordingWorkspaceModel) { self.model = model }

    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(verbatim: model.packageSheetTitle).font(.headline)
            Text("Select saved, independently verified takes and the room tone recorded with them. The lab service decides what is eligible.")
                .font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
            candidates
            if model.packageCandidatesTruncated {
                Text("More takes exist than this page shows; only the listed takes can be selected here.")
                    .font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
            }
            if let reason = model.packageSubmitUnavailableReason {
                Text(reason).accessibilityIdentifier("package.prerequisite")
                    .font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
            }
            if let status = model.packagePreparation { outcome(status) }
            HStack {
                Button("Cancel") { model.dismissPackageSheet() }.keyboardShortcut(.cancelAction)
                Spacer()
                Button("Prepare for lab") { model.submitPackagePreparation() }
                    .disabled(!model.canSubmitPackage).accessibilityIdentifier("package.submit")
                    .help("The lab service checks every prerequisite and explains anything missing")
            }
        }.padding(24).frame(width: 560).fixedSize(horizontal: false, vertical: true)
    }

    private var candidates: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(model.packageCandidates) { candidate in
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Toggle(isOn: Binding(
                            get: { model.packageSelectedTakeIDs.contains(candidate.takeID) },
                            set: { _ in model.togglePackageSelection(candidate.takeID) }
                        )) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(candidate.promptTitle).lineLimit(1)
                                HStack(spacing: 8) {
                                    if candidate.isRoomTone { label("Room tone") }
                                    label(candidate.decision)
                                    label(candidate.analysis)
                                    label(candidate.export)
                                    label(date(candidate))
                                }
                            }
                        }
                        .accessibilityIdentifier("package.take." + candidate.takeID)
                        Spacer()
                        if let reason = candidate.ineligibleReason {
                            Text(reason).font(.system(size: RecordingLayout.supportingTextSize))
                                .foregroundStyle(RecordingPalette.supportingText)
                        }
                    }
                }
            }
        }.frame(maxHeight: 220)
    }

    /// Each state dimension is its own label; none of them collapse into one busy flag.
    private func label(_ text: String) -> some View {
        Text(text).font(.system(size: RecordingLayout.supportingTextSize).monospacedDigit())
            .foregroundStyle(RecordingPalette.supportingText)
    }

    private func date(_ candidate: RecordingPackageCandidate) -> String {
        switch candidate.datePrecision {
        case "take": return candidate.recordedAt ?? "Unknown date"
        case "session": return (candidate.recordedAt ?? "Unknown date") + " (session)"
        default: return "Unknown date"
        }
    }

    @ViewBuilder private func outcome(_ status: RecordingPackagePreparationStatus) -> some View {
        switch status.phase {
        case .selecting: EmptyView()
        case .queued, .running:
            Text(status.statusLine ?? "Preparing lab package…").accessibilityIdentifier("package.status")
        case .blocked, .failed:
            VStack(alignment: .leading, spacing: 6) {
                Text(status.phase == .blocked ? "Preparation is blocked" : "Preparation stopped").font(.subheadline.weight(.semibold))
                // The service's own sentence is the whole explanation; it is never paraphrased.
                Text(status.message ?? "The lab service did not explain this failure")
                    .accessibilityIdentifier("package.blocked-message")
                if let code = status.errorCode { label(code) }
                if status.retryable && status.phase != .failed {
                    Button("Retry preparation") { model.submitPackagePreparation() }
                }
            }
        case .prepared:
            VStack(alignment: .leading, spacing: 6) {
                Text("Lab package prepared").font(.subheadline.weight(.semibold))
                    .accessibilityIdentifier("package.prepared")
                label((status.packagePath ?? "unknown path") + " · " + String(status.entryCount) + " recordings")
                if let manifest = status.manifestSHA256 { label(manifest) }
                Text("This is a prepared package, not accepted raw audio. It does not authorize transfer, synthesis, or model execution.")
                    .font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
            }
        }
    }
}
