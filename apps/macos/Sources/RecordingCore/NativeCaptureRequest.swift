import AudioToolbox
import CaptureBuffer
import CryptoKit
import Darwin
import Foundation
import RecordingContracts

/// Validated process inputs; Python supplies the canonical reservation digest.
/// This type extracts native device/path data without claiming canonical JSON ownership.
public struct NativeCaptureRequest: Sendable {
    public let reservation: RecordingJSON
    public let reservationSHA256: String
    public let recorderBuild: RecordingJSON
    public let assetRoot: URL
    public let takeID: String
    public let processGeneration: String
    public let deviceUID: String
    public let requestedAt: Date
    public let requestedAtText: String
    public let partialPath: String
    public let originalPath: String
    public let maximumFrames: UInt64
    public let bufferGeneration: UInt64

    public init(reservation: RecordingJSON, reservationSHA256: String,
                recorderBuild: RecordingJSON, assetRoot: URL, bufferGeneration: UInt64) throws {
        guard let object = reservation.object,
              object["schema_version"]?.string == "cole-voice-lab.capture-reservation/v1",
              let takeID = object["take_id"]?.string,
              Self.stableID(takeID),
              let generation = object["process_generation"]?.string, Self.stableID(generation),
              let device = object["group"]?.object?["capture_device"]?.object,
              device["backend"]?.string == "coreaudio",
              let uid = device["device_id"]?.string, !uid.isEmpty, uid.utf8.count <= 500,
              !uid.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              let requestedText = object["requested_at"]?.string,
              let requested = NativeCaptureTime.parse(requestedText),
              let maximum = object["maximum_frames"]?.integer,
              maximum > 0, maximum <= Int64(CVL_CAPTURE_MAXIMUM_FRAMES),
              object["partial_path"]?.string == "tmp/recording-takes/\(takeID)/capture.partial.wav",
              object["original_path"]?.string == "tmp/recording-takes/\(takeID)/original.wav",
              Self.digest(reservationSHA256),
              let build = recorderBuild.object,
              let buildID = build["build_id"]?.string, Self.stableID(buildID),
              ["source_sha256", "executable_sha256", "environment_sha256"].allSatisfy({
                  Self.digest(build[$0]?.string ?? "")
              }),
              assetRoot.isFileURL, assetRoot.path.hasPrefix("/"),
              !assetRoot.path.utf8.contains(0),
              !assetRoot.path.split(separator: "/").contains(where: { $0 == "." || $0 == ".." })
        else { throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Invalid native capture reservation or build identity") }
        self.reservation = reservation
        self.reservationSHA256 = reservationSHA256
        self.recorderBuild = recorderBuild
        self.assetRoot = assetRoot
        self.takeID = takeID
        self.processGeneration = generation
        self.deviceUID = uid
        self.requestedAt = requested
        self.requestedAtText = requestedText
        self.partialPath = "tmp/recording-takes/\(takeID)/capture.partial.wav"
        self.originalPath = "tmp/recording-takes/\(takeID)/original.wav"
        self.maximumFrames = UInt64(maximum)
        self.bufferGeneration = bufferGeneration
    }

    private static func stableID(_ text: String) -> Bool {
        text.range(of: "^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$", options: .regularExpression) != nil
    }

    private static func digest(_ text: String) -> Bool {
        text.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil
    }
}

/// Error codes match Python's RecordingError; internal C detail remains non-sensitive.
public struct NativeCaptureFailure: Error, Equatable, Sendable {
    public let code: String
    public let message: String
    public let details: [String]
    public let retryable: Bool

    public init(_ code: String, _ message: String, details: [String] = [], retryable: Bool = true) {
        self.code = code
        self.message = message
        self.details = details
        self.retryable = retryable
    }

    public var json: RecordingJSON {
        .object(["code": .string(code), "message": .string(message),
                 "details": .array(details.map(RecordingJSON.string)), "retryable": .boolean(retryable)])
    }

    static func system(_ operation: String, errno value: Int32 = Darwin.errno) -> Self {
        Self("RECORDING.WRITE_FAILED", "Native capture \(operation) failed", details: ["errno=\(value)"])
    }

    static func capture(_ snapshot: CVLCaptureSnapshot) -> Self? {
        guard snapshot.error != CVL_CAPTURE_OK else { return nil }
        let code: String
        switch snapshot.error {
        case CVL_CAPTURE_RAW_OVERFLOW: code = "RECORDING.CAPTURE_OVERFLOW"
        case CVL_CAPTURE_INVALID_TIMESTAMP, CVL_CAPTURE_TIMESTAMP_DISCONTINUITY,
             CVL_CAPTURE_PARTIAL_PCM_FRAME, CVL_CAPTURE_INVALID_BUFFER: code = "RECORDING.CONTINUITY_FAILED"
        case CVL_CAPTURE_DEVICE_CHANGED, CVL_CAPTURE_FORMAT_CHANGED, CVL_CAPTURE_ENQUEUE_FAILED:
            code = "RECORDING.DEVICE_UNAVAILABLE"
        case CVL_CAPTURE_PARENT_LOST: code = "RECORDING.PARENT_LOST"
        default: code = "RECORDING.WRITE_FAILED"
        }
        return Self(code, "Native capture stopped with an explicit failure",
                    details: [String(cString: cvl_capture_error_code(snapshot.error))])
    }
}

/// Clocks are injectable for synthetic tests; production host timestamps use mach_absolute_time.
public struct NativeCaptureClock: Sendable {
    public let now: @Sendable () -> Date
    public let monotonicSeconds: @Sendable () -> Double
    public let hostTicks: @Sendable () -> UInt64
    public let hostSecondsPerTick: Double

    public init(now: @escaping @Sendable () -> Date,
                monotonicSeconds: @escaping @Sendable () -> Double,
                hostTicks: @escaping @Sendable () -> UInt64, hostSecondsPerTick: Double) {
        self.now = now
        self.monotonicSeconds = monotonicSeconds
        self.hostTicks = hostTicks
        self.hostSecondsPerTick = hostSecondsPerTick
    }

    public static var system: Self {
        var info = mach_timebase_info_data_t()
        mach_timebase_info(&info)
        let secondsPerTick = Double(info.numer) / Double(info.denom) / 1_000_000_000
        return Self(now: { Date() },
                    monotonicSeconds: { Double(mach_continuous_time()) * secondsPerTick },
                    hostTicks: { mach_absolute_time() }, hostSecondsPerTick: secondsPerTick)
    }
}

public typealias NativeCaptureTime = RecordingTimestamp

public struct NativeCaptureOutcome: Sendable {
    public let recovery: RecordingJSON
    public let seal: RecordingJSON?
    public let failure: NativeCaptureFailure?
    public let recoveryPersisted: Bool
}
