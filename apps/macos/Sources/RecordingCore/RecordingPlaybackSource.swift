import Foundation
import RecordingContracts

/// The one PCM WAV layout a playback source declares; the backend refuses any other file.
/// `capture`: a native take (48 kHz, 24-bit, mono). `speech`: a Speak clip the service wrote
/// (24 kHz, 16-bit, mono).
public enum RecordingPlaybackFormat: String, Equatable, Sendable {
    case capture, speech

    public var sampleRate: Double {
        switch self {
        case .capture: 48_000
        case .speech: 24_000
        }
    }

    public var bitsPerSample: UInt32 {
        switch self {
        case .capture: 24
        case .speech: 16
        }
    }

    public var channels: UInt32 { 1 }
    public var bytesPerFrame: Int64 { Int64(bitsPerSample / 8) * Int64(channels) }

    /// The largest file a source of `frameCount` frames may be: its samples plus header room.
    public func maximumFileBytes(frameCount: Int64) -> Int64 { frameCount * bytesPerFrame + 1_048_576 }
}

/// A Python-authorized selected source, never a request to inspect the library.
/// `takeID` is the stable id the controller keys its cursor memory by: a take's id, or a
/// Speak request's operation id.
public struct RecordingPlaybackSource: Equatable, Sendable {
    public let takeID: String
    public let sourceSHA256: String
    public let assetRoot: URL
    public let relativePath: String
    public let frameCount: Int64
    public let fileIdentity: RecordingJSON?
    public let format: RecordingPlaybackFormat

    /// Throws: RECORDING.INVALID_MESSAGE unless the id is stable, the digest is SHA-256 hex, the
    /// root is an absolute file URL, the path is confined and the frame count is 1...28,800,000.
    public init(takeID: String, sourceSHA256: String, assetRoot: URL, relativePath: String, frameCount: Int64,
                fileIdentity: RecordingJSON? = nil, format: RecordingPlaybackFormat = .capture) throws {
        guard takeID.range(of: "^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$", options: .regularExpression) != nil,
              sourceSHA256.range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
              assetRoot.isFileURL, assetRoot.path.hasPrefix("/"),
              !relativePath.isEmpty, !relativePath.hasPrefix("/"), !relativePath.contains("\\"),
              !relativePath.unicodeScalars.contains(where: CharacterSet.controlCharacters.contains),
              relativePath.split(separator: "/", omittingEmptySubsequences: false).allSatisfy({ !["", ".", ".."].contains($0) }),
              frameCount > 0, frameCount <= 28_800_000 else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Playback requires a confined checksum-bound source and exact frame count")
        }
        self.takeID = takeID
        self.sourceSHA256 = sourceSHA256
        self.assetRoot = assetRoot
        self.relativePath = relativePath
        self.frameCount = frameCount
        self.fileIdentity = fileIdentity
        self.format = format
    }
}

public enum RecordingPlaybackBackendEvent: Sendable {
    case finished(generation: UInt64)
    case failed(generation: UInt64, error: RecordingContractError)
}

/// One stream/engine per controller. Test adapters never open an output device.
public protocol RecordingPlaybackBackend: Sendable {
    func setEventHandler(_ handler: @escaping @Sendable (RecordingPlaybackBackendEvent) async -> Void) async
    func load(_ source: RecordingPlaybackSource, generation: UInt64) async throws
    func load(_ source: RecordingPlaybackSource, generation: UInt64, diagnosticContext: RecordingDiagnosticContext?) async throws
    func play(from frame: Int64, through endFrame: Int64, generation: UInt64) async throws
    func pause() async throws -> Int64?
    func position() async -> Int64?
    func stop() async throws
    func recover() async throws
}

public extension RecordingPlaybackBackend {
    /// Existing adapters remain inert; the production reader carries explicit attribution.
    func load(_ source: RecordingPlaybackSource, generation: UInt64, diagnosticContext: RecordingDiagnosticContext?) async throws {
        try await load(source, generation: generation)
    }
}
