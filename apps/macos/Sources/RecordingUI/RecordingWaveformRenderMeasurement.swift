import Foundation
import RecordingCore

/// Actual Canvas work bound to the exact immutable snapshot it drew. This is render
/// completion evidence, not a claim about physical display scanout or compositor latency.
public struct RecordingWaveformRenderMeasurement: Sendable {
    public let workSeconds: Double
    public let renderCompletedHostTicks: Int64
    public let renderedSnapshot: RecordingWaveformSnapshot?
}
