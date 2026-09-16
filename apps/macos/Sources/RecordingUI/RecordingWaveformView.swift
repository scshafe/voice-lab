import SwiftUI
import Darwin
import RecordingCore

/// Display-only logarithmic amplitude axis. Real UMIK-1 speech peaks near −30 dBFS drew
/// visually flat on a linear full scale. Extrema, frame accounting and snapshots are unchanged.
public enum RecordingWaveformAmplitudeScale {
    /// 0 dBFS reaches the full half-height; this level and below draw as the floor hairline.
    public static let floorDecibels = -60.0
    /// Half-height fraction for the hairline: 0.5 point per side on the minimum 80-point plot.
    public static let floorMagnitude = 1.0 / 80
    public static let label = "dB display scale"

    /// Signed full-scale amplitude to a signed, monotonic half-height fraction: 0 dBFS → ±1,
    /// −30 dBFS → ±0.5. Exact zero and NaN stay on the baseline; nothing fabricates height.
    public static func displayFraction(amplitude: Double) -> Double {
        guard !amplitude.isNaN, amplitude != 0 else { return 0 }
        let decibels = 20 * log10(min(1, abs(amplitude)))
        let magnitude = max(floorMagnitude, 1 - decibels / floorDecibels)
        return amplitude < 0 ? -magnitude : magnitude
    }

    /// Explicit display gain applies to source amplitude before the logarithmic mapping.
    public static func y(amplitude: Double, displayGain: Double, coordinates: RecordingWaveformCoordinates) -> CGFloat {
        coordinates.y(amplitude: displayFraction(amplitude: amplitude * displayGain))
    }
}

/// Native sample-based waveform. Canvas receives an immutable ≤4096-bucket snapshot;
/// drawing, occlusion and gestures never read audio or hold the capture writer's lock.
@MainActor
public struct RecordingWaveformView: View {
    @ObservedObject private var model: RecordingWaveformModel
    private let isRecording: Bool
    private let viewport: Range<Int64>?
    private let fitting: Bool
    private let energySaving: Bool
    private let positionFrame: Int64?
    private let trimRange: Range<Int64>?
    private let onSeek: (Int64) -> Void
    private let onTrim: (Range<Int64>) -> Void
    private let renderObserver: ((RecordingWaveformRenderMeasurement) -> Void)?
    @State private var dragTarget: String?

    public init(model: RecordingWaveformModel, isRecording: Bool, viewport: Range<Int64>? = nil,
                fitting: Bool = false, energySaving: Bool = false, positionFrame: Int64? = nil,
                trimRange: Range<Int64>? = nil, onSeek: @escaping (Int64) -> Void = { _ in },
                onTrim: @escaping (Range<Int64>) -> Void = { _ in }, renderObserver: ((RecordingWaveformRenderMeasurement) -> Void)? = nil) {
        self.model = model; self.isRecording = isRecording; self.viewport = viewport
        self.fitting = fitting; self.energySaving = energySaving; self.positionFrame = positionFrame
        self.trimRange = trimRange; self.onSeek = onSeek; self.onTrim = onTrim; self.renderObserver = renderObserver
    }

    public var body: some View {
        let snapshot = model.snapshot
        let written = snapshot?.writtenFrames ?? 0
        let frames = viewport ?? (isRecording ? RecordingWaveformCoordinates.liveViewport(writtenFrames: written) : 0..<max(1, written))
        let gain = RecordingWaveformCoordinates.displayGain(snapshot: snapshot, fitting: fitting, recording: isRecording)
        TimelineView(.animation(minimumInterval: energySaving ? 1 / 30 : 1 / 60, paused: !isRecording)) { _ in
            Canvas(rendersAsynchronously: false) { context, size in
                let started = ContinuousClock.now
                let plot = CGRect(x: 12, y: 16, width: max(1, size.width - 24), height: max(1, size.height - 40))
                guard let coordinates = try? RecordingWaveformCoordinates(viewport: frames, plot: plot) else { return }
                // The plot is a content surface: the system text background follows the effective appearance.
                context.fill(Path(CGRect(origin: .zero, size: size)), with: .color(Color(nsColor: .textBackgroundColor)))
                var baseline = Path(); baseline.move(to: CGPoint(x: plot.minX, y: plot.midY)); baseline.addLine(to: CGPoint(x: plot.maxX, y: plot.midY))
                context.stroke(baseline, with: .color(.secondary.opacity(0.3)), lineWidth: 1)
                var envelope = Path()
                if let snapshot {
                    for (index, bucket) in snapshot.buckets.enumerated() {
                        let first = snapshot.firstFrame + Int64(index) * snapshot.bucketWidthFrames
                        let end = min(first + snapshot.bucketWidthFrames, snapshot.endFrame)
                        guard end > frames.lowerBound, first < frames.upperBound else { continue }
                        let x = coordinates.x(frame: first + (end - first) / 2)
                        // Exact silence adds no fake waveform height; the neutral baseline remains visible.
                        if bucket.minimum != 0 || bucket.maximum != 0 {
                            if bucket.minimum == bucket.maximum {
                                let y = RecordingWaveformAmplitudeScale.y(amplitude: bucket.lowAmplitude, displayGain: gain, coordinates: coordinates)
                                envelope.move(to: CGPoint(x: coordinates.x(frame: first), y: y))
                                envelope.addLine(to: CGPoint(x: coordinates.x(frame: end), y: y))
                            } else {
                                envelope.move(to: CGPoint(x: x, y: RecordingWaveformAmplitudeScale.y(amplitude: bucket.lowAmplitude, displayGain: gain, coordinates: coordinates)))
                                envelope.addLine(to: CGPoint(x: x, y: RecordingWaveformAmplitudeScale.y(amplitude: bucket.highAmplitude, displayGain: gain, coordinates: coordinates)))
                            }
                        }
                    }
                }
                var clipped = context; clipped.clip(to: Path(plot))
                clipped.stroke(envelope, with: .color(isRecording ? .red : .accentColor), lineWidth: 1)
                if let trimRange, !isRecording {
                    for frame in [trimRange.lowerBound, trimRange.upperBound] {
                        let x = coordinates.x(frame: frame)
                        var grip = Path(); grip.move(to: CGPoint(x: x, y: plot.minY)); grip.addLine(to: CGPoint(x: x, y: plot.maxY))
                        context.stroke(grip, with: .color(.orange), lineWidth: 3)
                    }
                }
                let playhead = positionFrame ?? (isRecording ? written : 0)
                if frames.contains(playhead) || playhead == frames.upperBound {
                    let x = coordinates.x(frame: playhead)
                    var cursor = Path(); cursor.move(to: CGPoint(x: x, y: plot.minY)); cursor.addLine(to: CGPoint(x: x, y: plot.maxY))
                    context.stroke(cursor, with: .color(isRecording ? .red : .primary.opacity(0.7)), lineWidth: 1)
                }
                let seconds = Double(written) / 48_000
                let label = String(format: "%.3f s", seconds) + (gain > 1 ? String(format: "  ·  Display only ×%.1f", gain) : "")
                context.draw(Text(label).font(.system(size: RecordingLayout.supportingTextSize, design: .monospaced)).foregroundColor(RecordingPalette.supportingText), at: CGPoint(x: plot.minX, y: size.height - 12), anchor: .leading)
                // The scale label shares the existing label line; it adds no geometry region.
                context.draw(Text(RecordingWaveformAmplitudeScale.label).font(.system(size: RecordingLayout.supportingTextSize, design: .monospaced)).foregroundColor(RecordingPalette.supportingText), at: CGPoint(x: plot.maxX, y: size.height - 12), anchor: .trailing)
                let elapsed = started.duration(to: .now).components
                let ticks = mach_absolute_time()
                if ticks <= UInt64(Int64.max) {
                    renderObserver?(RecordingWaveformRenderMeasurement(
                        workSeconds: Double(elapsed.seconds) + Double(elapsed.attoseconds) / 1e18,
                        renderCompletedHostTicks: Int64(ticks), renderedSnapshot: snapshot))
                }
            }

        }
        .overlay {
            GeometryReader { geometry in
                Color.clear.contentShape(Rectangle()).gesture(DragGesture(minimumDistance: 0).onChanged { value in
                    guard !isRecording, written > 0,
                          let coordinates = try? RecordingWaveformCoordinates(viewport: frames, plot: CGRect(x: 12, y: 16, width: max(1, geometry.size.width - 24), height: max(1, geometry.size.height - 40))) else { return }
                    if dragTarget == nil {
                        if let trimRange, abs(value.startLocation.x - coordinates.x(frame: trimRange.lowerBound)) <= 16 { dragTarget = "start" }
                        else if let trimRange, abs(value.startLocation.x - coordinates.x(frame: trimRange.upperBound)) <= 16 { dragTarget = "end" }
                        else { dragTarget = "seek" }
                    }
                    let frame = coordinates.frame(x: value.location.x, retainedRange: 0..<written)
                    if let trimRange, dragTarget == "start" { onTrim(min(frame, trimRange.upperBound - 1)..<trimRange.upperBound) }
                    else if let trimRange, dragTarget == "end" { onTrim(trimRange.lowerBound..<max(frame, trimRange.lowerBound + 1)) }
                    else { onSeek(coordinates.frame(x: value.location.x, retainedRange: trimRange)) }
                }.onEnded { _ in dragTarget = nil })
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Original recording waveform")
        .accessibilityValue(isRecording ? "Recording" : "\(String(format: "%.3f", Double(written) / 48_000)) seconds")
        .accessibilityHint(isRecording ? "Amplitude uses a display-only decibel scale" : "Seek or adjust trim handles in source frames")
    }
}
