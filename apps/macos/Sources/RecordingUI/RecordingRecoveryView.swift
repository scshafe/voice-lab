import SwiftUI

/// Names the records an interrupted session left behind, in the service's own words.
/// It offers a re-check and nothing else: recovery never re-records, never keeps a draft,
/// and never presents an incomplete capture as a successful one.
@MainActor
public struct RecordingRecoveryView: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    public init(model: RecordingWorkspaceModel) { self.model = model }

    public var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Recordings that need attention").font(.headline)
            Text("Preserved captures are checked in the background. Pending checks do not mean a recording is damaged. Original material is retained.")
                .font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
            if let supervisor = model.recoverySupervisorError {
                Text(supervisor).accessibilityIdentifier("recovery.supervisor-error")
            }
            if model.recoveryScanState == "scanning" {
                Text(model.recoveryStatusLine ?? "Checking preserved recordings…")
                    .accessibilityIdentifier("recovery.progress")
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    if model.recoveryIncompleteRows.isEmpty {
                        Text(model.recoveryEmptyMessage).accessibilityIdentifier("recovery.empty")
                    }
                    ForEach(model.recoveryIncompleteRows) { row in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(spacing: 8) {
                                Text(row.status).font(.system(size: 13, weight: .medium))
                                if let code = row.errorCode {
                                    Text(code).font(.system(size: RecordingLayout.supportingTextSize).monospacedDigit())
                                        .foregroundStyle(RecordingPalette.supportingText)
                                }
                                Spacer()
                                Button("Check again") { Task { await model.recoverTake(row.takeID) } }
                                    .disabled(!model.canRecheckRecoveryTake(row.takeID))
                            }
                            Text(row.errorMessage ?? "This record has no further detail")
                                .font(.system(size: RecordingLayout.supportingTextSize))
                                .foregroundStyle(RecordingPalette.supportingText)
                        }
                        .accessibilityIdentifier("recovery.take." + row.takeID)
                    }
                }
            }.frame(maxHeight: 220)
            HStack {
                Button("Previous") { model.previousRecoveryPage() }
                    .disabled(model.recoveryInventoryLoading || model.recoveryPreviousCursors.isEmpty)
                    .accessibilityIdentifier("recovery.previous")
                Button("Next") { model.nextRecoveryPage() }
                    .disabled(model.recoveryInventoryLoading || model.recoveryNextCursor == nil)
                    .accessibilityIdentifier("recovery.next")
                Spacer()
                Button("Done") { model.dismissRecoverySheet() }.keyboardShortcut(.cancelAction)
            }
        }.padding(24).frame(width: 520).fixedSize(horizontal: false, vertical: true)
    }
}
