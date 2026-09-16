import RecordingCore
import SwiftUI

/// Sample count during capture; source cursor and retained duration during review.
@MainActor
public struct RecordingMediaTimer: View {
    @ObservedObject private var model: RecordingWorkspaceModel
    @ObservedObject private var waveform: RecordingWaveformModel
    @ObservedObject private var review: RecordingReviewController
    @ObservedObject private var playback: RecordingPlaybackController
    public init(model: RecordingWorkspaceModel) {
        self.model = model; waveform = model.waveform; review = model.review; playback = model.review.playback
    }
    public var body: some View {
        let reviewing = model.selectedReview != nil && !model.state.showsStop
        let duration = Self.durationFrames(reviewing: reviewing,
            writtenFrames: waveform.snapshot?.writtenFrames ?? 0, retainedRange: review.trimRange)
        let frames = reviewing ? max(0, playback.cursorFrame - review.trimRange.lowerBound) : duration
        let value = formatted(frames) + (reviewing ? " / " + formatted(duration) : "")
        Text(value).font(.system(size: reviewing ? 16 : 20, design: .monospaced)).monospacedDigit()
            .accessibilityLabel(reviewing ? "Playback position and retained duration" : "Recorded duration")
            .accessibilityValue(value).accessibilityIdentifier("recording.timer")
    }
    /// Canonical frame input to the displayed duration; review's cursor is a separate value.
    nonisolated public static func durationFrames(reviewing: Bool, writtenFrames: Int64,
                                                  retainedRange: Range<Int64>) -> Int64 {
        reviewing ? Int64(retainedRange.count) : writtenFrames
    }
    private func formatted(_ frames: Int64) -> String {
        let hundredths = frames / 480
        return String(format: "%02lld:%02lld.%02lld", hundredths / 6000, (hundredths / 100) % 60, hundredths % 100)
    }
}
