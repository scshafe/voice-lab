import Foundation
import CoreGraphics
import RecordingContracts

/// @canonical recording_waveform_coordinates -- source frames for waveform, seek and trim.
public struct RecordingWaveformCoordinates: Sendable {
    public let viewport: Range<Int64>
    public let plot: CGRect

    public init(viewport: Range<Int64>, plot: CGRect) throws {
        guard viewport.lowerBound >= 0, viewport.upperBound <= RecordingWaveformCache.maximumFrames,
              viewport.upperBound > viewport.lowerBound, plot.width.isFinite, plot.height.isFinite,
              plot.minX.isFinite, plot.minY.isFinite, plot.width > 0, plot.height > 0 else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Waveform coordinates need finite positive geometry and ordered source-frame bounds")
        }
        self.viewport = viewport; self.plot = plot
    }

    public func x(frame: Int64) -> CGFloat {
        plot.minX + CGFloat(frame - viewport.lowerBound) / CGFloat(viewport.count) * plot.width
    }

    public func frame(x: CGFloat, retainedRange: Range<Int64>? = nil) -> Int64 {
        guard x.isFinite else { return retainedRange?.lowerBound ?? viewport.lowerBound }
        let fraction = max(0, min(1, (x - plot.minX) / plot.width))
        let source = viewport.lowerBound + Int64((fraction * CGFloat(viewport.count)).rounded())
        let retained = retainedRange ?? viewport
        return max(retained.lowerBound, min(retained.upperBound, source))
    }

    public func y(amplitude: Double, displayGain: Double = 1) -> CGFloat {
        plot.midY - CGFloat(max(-1, min(1, amplitude * displayGain))) * plot.height / 2
    }

    /// Live view uses only the confirmed written-frame clock and reserves a stable time span.
    public static func liveViewport(writtenFrames: Int64, recentFrames: Int64 = 192_000) -> Range<Int64> {
        let end = max(recentFrames, writtenFrames)
        return max(0, end - recentFrames)..<end
    }

    public static func displayGain(snapshot: RecordingWaveformSnapshot?, fitting: Bool, recording: Bool) -> Double {
        guard fitting, !recording, let peak = snapshot?.peakAmplitude, peak > 0 else { return 1 }
        return max(1, min(64, 1 / peak))
    }
}
