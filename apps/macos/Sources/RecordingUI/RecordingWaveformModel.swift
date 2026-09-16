import Foundation
import RecordingCore

/// The waveform alone observes visual traffic. Old process/take snapshots cannot replace selection.
@MainActor
public final class RecordingWaveformModel: ObservableObject {
    @Published public private(set) var snapshot: RecordingWaveformSnapshot?
    public private(set) var takeID: String
    public private(set) var processGeneration: String
    public private(set) var rejectedSnapshotCount = 0
    private var hostClockComparable: Bool

    public init(takeID: String, processGeneration: String, hostClockComparable: Bool = true) {
        self.takeID = takeID; self.processGeneration = processGeneration; self.hostClockComparable = hostClockComparable
    }

    /// Select atomically with prompt/take state; history/reconnect has no assumed host-clock binding.
    public func select(takeID: String, processGeneration: String, hostClockComparable: Bool) {
        self.takeID = takeID; self.processGeneration = processGeneration
        self.hostClockComparable = hostClockComparable
        snapshot = nil
    }

    /// Install an already-validated selected cache without publishing an empty intermediate frame.
    public func install(_ next: RecordingWaveformSnapshot, hostClockComparable: Bool) {
        takeID = next.takeID; processGeneration = next.processGeneration
        self.hostClockComparable = hostClockComparable; snapshot = next
    }

    @discardableResult public func accept(_ next: RecordingWaveformSnapshot) -> Bool {
        guard next.takeID == takeID, next.processGeneration == processGeneration,
              next.writtenFrames >= (snapshot?.writtenFrames ?? 0) else {
            rejectedSnapshotCount += 1; return false
        }
        snapshot = next
        return true
    }

    public func capturedSampleAge(visibleHostTicks: Int64) throws -> Double? {
        guard hostClockComparable, let snapshot, snapshot.endFrame > 0, let clock = snapshot.sampleClock else { return nil }
        return try clock.ageSeconds(frame: snapshot.endFrame - 1, visibleHostTicks: visibleHostTicks)
    }
}
