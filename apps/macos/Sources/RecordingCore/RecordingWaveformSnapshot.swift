import Foundation
import RecordingContracts

/// Signed 24-bit source extrema; zero amplitude stays on the baseline.
public struct RecordingWaveformExtrema: Equatable, Sendable {
    public let minimum: Int32
    public let maximum: Int32
    public init(minimum: Int32, maximum: Int32) { self.minimum = minimum; self.maximum = maximum }
    public var lowAmplitude: Double { Double(minimum) / 8_388_608 }
    public var highAmplitude: Double { Double(maximum) / 8_388_608 }
    static func merge(_ left: Self, _ right: Self) -> Self {
        Self(minimum: min(left.minimum, right.minimum), maximum: max(left.maximum, right.maximum))
    }
}

/// Actual captured-sample/host-clock binding, independent of visualization scheduling.
public struct RecordingWaveformSampleClock: Equatable, Sendable {
    public let anchorFrame: Int64
    public let hostTimeTicks: Int64
    public let secondsPerTick: Double
    public init(anchorFrame: Int64, hostTimeTicks: Int64, secondsPerTick: Double) throws {
        guard anchorFrame >= 0, hostTimeTicks >= 0, secondsPerTick.isFinite, secondsPerTick > 0 else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Waveform sample clock must have nonnegative identities and a finite positive scale")
        }
        self.anchorFrame = anchorFrame; self.hostTimeTicks = hostTimeTicks; self.secondsPerTick = secondsPerTick
    }
    /// Captured-sample age on the shared host clock; a future sample is invalid evidence.
    public func ageSeconds(frame: Int64, visibleHostTicks: Int64) throws -> Double {
        guard frame >= anchorFrame, visibleHostTicks >= hostTimeTicks else {
            throw RecordingContractError(code: "RECORDING.CONTINUITY_FAILED", message: "Visible waveform sample precedes its actual host clock anchor")
        }
        let age = Double(visibleHostTicks - hostTimeTicks) * secondsPerTick - Double(frame - anchorFrame) / 48_000
        guard age >= -secondsPerTick else {
            throw RecordingContractError(code: "RECORDING.CONTINUITY_FAILED", message: "Waveform display claims a sample from the future")
        }
        return max(0, age)
    }
}

/// Immutable bounded visual projection; rendering cannot hold capture/cache locks.
public struct RecordingWaveformSnapshot: Sendable {
    public let takeID: String
    public let processGeneration: String
    public let firstFrame: Int64
    public let endFrame: Int64
    public let writtenFrames: Int64
    public let bucketWidthFrames: Int64
    public let buckets: [RecordingWaveformExtrema]
    public let sampleClock: RecordingWaveformSampleClock?
    public var durationSeconds: Double { Double(writtenFrames) / 48_000 }
    public var peakAmplitude: Double { buckets.reduce(0) { max($0, abs($1.lowAmplitude), abs($1.highAmplitude)) } }

    public var wireData: RecordingJSON {
        .object([
            "event": .string("waveform"), "take_id": .string(takeID), "process_generation": .string(processGeneration),
            "first_frame": .integer(firstFrame), "end_frame": .integer(endFrame), "written_frames": .integer(writtenFrames),
            "bucket_width_frames": .integer(bucketWidthFrames),
            "buckets": .array(buckets.map { .object(["minimum": .number($0.lowAmplitude), "maximum": .number($0.highAmplitude)]) }),
            "sample_clock": sampleClock.map { .object([
                "anchor_frame": .integer($0.anchorFrame), "host_time_ticks": .integer($0.hostTimeTicks), "seconds_per_tick": .number($0.secondsPerTick),
            ]) } ?? .null,
        ])
    }
}

extension RecordingWaveformSnapshot {
    /// Decode the strict telemetry boundary once, then project to immutable native rendering values.
    public static func decode(_ payload: Data, schema: Data) throws -> RecordingWaveformSnapshot {
        try project(RecordingWireEnvelope.decode(payload, schema: schema))
    }

    /// Project an already strictly validated channel envelope without reparsing/reencoding JSON.
    public static func project(_ envelope: RecordingWireEnvelope) throws -> RecordingWaveformSnapshot {
        guard let data = envelope.data.object, data["event"] == .string("waveform"),
              let takeID = data["take_id"]?.string, let generation = data["process_generation"]?.string,
              let first = data["first_frame"]?.integer, let end = data["end_frame"]?.integer,
              let written = data["written_frames"]?.integer, let width = data["bucket_width_frames"]?.integer,
              let buckets = data["buckets"]?.array else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Expected a strictly validated waveform telemetry envelope")
        }
        func amplitude(_ value: RecordingJSON?) throws -> Int32 {
            let number: Double
            switch value { case .number(let value): number = value; case .integer(let value): number = Double(value); default: throw invalid() }
            guard number.isFinite, (-1...1).contains(number) else { throw invalid() }
            return Int32(max(-8_388_608, min(8_388_607, (number * 8_388_608).rounded())))
        }
        func invalid() -> RecordingContractError { RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Invalid waveform projection after schema validation") }
        let extrema = try buckets.map { try RecordingWaveformExtrema(minimum: amplitude($0.object?["minimum"]), maximum: amplitude($0.object?["maximum"])) }
        var clock: RecordingWaveformSampleClock?
        if let object = data["sample_clock"]?.object {
            guard let anchor = object["anchor_frame"]?.integer, let ticks = object["host_time_ticks"]?.integer else { throw invalid() }
            let seconds: Double
            switch object["seconds_per_tick"] { case .number(let value): seconds = value; case .integer(let value): seconds = Double(value); default: throw invalid() }
            clock = try RecordingWaveformSampleClock(anchorFrame: anchor, hostTimeTicks: ticks, secondsPerTick: seconds)
        }
        return RecordingWaveformSnapshot(takeID: takeID, processGeneration: generation, firstFrame: first,
            endFrame: end, writtenFrames: written, bucketWidthFrames: width, buckets: extrema, sampleClock: clock)
    }
}
