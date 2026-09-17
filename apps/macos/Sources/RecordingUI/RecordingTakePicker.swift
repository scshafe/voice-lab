import Foundation
import RecordingContracts
import RecordingCore
import SwiftUI

@MainActor
public struct RecordingTakePicker: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    @ObservedObject private var review: RecordingReviewController
    @State private var showingHistory = false
    public init(model: RecordingWorkspaceModel) { self.model = model; review = model.review }
    public var body: some View {
        HStack {
            // A reservation that never sealed is not presented as a draft awaiting checks.
            Label(model.selectedDecision == "draft" && !model.state.reservationUnsealed ? "Current draft" : "Selected take", systemImage: "waveform")
            if let source = model.selectedReview {
                Text(date(source.recordedAt, precision: source.datePrecision)).foregroundStyle(RecordingPalette.supportingText).lineLimit(1)
            }
            Spacer(minLength: 8)
            Text(review.loading ? "Loading review…" : analysisTitle(model.state.reservationUnsealed ? nil : model.state.analysis ?? model.selectedReview?.analysis)).foregroundStyle(RecordingPalette.supportingText)
            Button { showingHistory = true; Task { await model.loadTakeHistory(reset: true) } } label: { Image(systemName: "chevron.down") }
                .buttonStyle(.borderless).frame(width: 22).disabled(!model.canSelectTake)
                .accessibilityLabel("Select dated take").accessibilityIdentifier("review.take-picker")
                .popover(isPresented: $showingHistory) { history }
        }.font(.system(size: 13))
    }
    private var history: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Takes · \(model.state.prompt?.title ?? "Selected prompt")").font(.headline)
            if model.takeHistoryLoading { Text("Loading take history…").foregroundStyle(RecordingPalette.supportingText) }
            // Orange text falls below 4.5:1 on either surface; the glyph carries the warning colour instead.
            if let error = model.takeHistoryError { HStack(spacing: 6) { Image(systemName: "exclamationmark.circle").foregroundStyle(.orange); Text(error) } }
            ScrollView {
                LazyVStack(spacing: 4) {
                    ForEach(Array(model.visibleTakeHistory.enumerated()), id: \.offset) { _, take in
                        if let id = take.object?["take_id"]?.string {
                            Button {
                                showingHistory = false; model.selectTake(id)
                            } label: {
                                HStack {
                                    VStack(alignment: .leading, spacing: 3) {
                                        Text(date(take.object?["recorded_at"]?.string, precision: take.object?["date_precision"]?.string ?? "unknown"))
                                        Text("\(take.object?["decision"]?.string ?? "unknown") · \(analysisTitle(take.object?["analysis"]?.string))")
                                            .font(.system(size: RecordingLayout.supportingTextSize)).foregroundStyle(RecordingPalette.supportingText)
                                    }
                                    Spacer()
                                    if id == model.state.takeID { Text("Current").font(.system(size: RecordingLayout.supportingTextSize)) }
                                }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
                            }.buttonStyle(.plain).disabled(!model.canSelectTake)
                                .accessibilityIdentifier("review.take." + id)
                        }
                    }
                    if model.visibleTakeHistory.isEmpty && !model.takeHistoryLoading && model.takeHistoryError == nil { Text("No indexed takes for this prompt").foregroundStyle(RecordingPalette.supportingText) }
                }
            }
            HStack {
                Button("Previous") { model.previousTakeHistoryPage() }.disabled(model.takeHistoryPreviousCursors.isEmpty || model.takeHistoryLoading)
                Button("Next") { model.nextTakeHistoryPage() }.disabled(model.takeHistoryNextCursor == nil || model.takeHistoryLoading)
                Spacer()
                Button("Done") { showingHistory = false }.keyboardShortcut(.cancelAction)
            }
        }.padding(16).frame(width: 440, height: 350)
    }
    private func date(_ text: String?, precision: String) -> String {
        Self.recordedDate(text, precision: precision, locale: .current, timeZone: .current)
    }
    /// When a take was recorded. A take-precise time shows to the millisecond; a take whose
    /// time is known only from when its recording began shows its date alone.
    nonisolated static func recordedDate(_ text: String?, precision: String, locale: Locale, timeZone: TimeZone) -> String {
        guard let text, let value = RecordingTimestamp.parse(text) else { return "Date unknown" }
        let formatter = DateFormatter(); formatter.locale = locale; formatter.timeZone = timeZone
        guard precision == "take" else {
            formatter.dateFormat = "MMM d, yyyy"
            return formatter.string(from: value) + " · exact time unknown"
        }
        formatter.dateFormat = "MMM d, yyyy 'at' h:mm:ss.SSS a"
        return formatter.string(from: value)
    }
    private func analysisTitle(_ value: String?) -> String {
        switch value {
        case "pending", "running": "Checking"
        case "verified": "Verified"
        case "invalid": "Invalid take"
        case "needs-review": "Needs review"
        case "failed": "Check failed"
        default: "No recorded take"
        }
    }
}
