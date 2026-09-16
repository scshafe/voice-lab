import Foundation
import RecordingContracts
import RecordingContracts

/// A Python-authorized selected source, never a request to inspect the library.
public struct RecordingPlaybackSource: Equatable, Sendable {
    public let takeID: String
    public let sourceSHA256: String
    public let assetRoot: URL
    public let relativePath: String
    public let frameCount: Int64
    public let fileIdentity: RecordingJSON?

    public init(takeID: String, sourceSHA256: String, assetRoot: URL, relativePath: String, frameCount: Int64, fileIdentity: RecordingJSON? = nil) throws {
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
