import CryptoKit
import Foundation
import RecordingContracts
import RecordingCore

/// Explicit helper process configuration; synthetic input cannot activate hardware.
struct RecordingHelperConfiguration: Sendable {
    let assetRoot: URL
    let generation: String
    let build: RecordingJSON
    let expectedClientBuild: RecordingJSON
    let schemaDirectory: URL
    let syntheticPCM: Data?
    let syntheticChunkFrames: Int
    let syntheticTotalFrames: UInt64?
    let syntheticSealBarrier: String?
    let syntheticStartBarrier: String?

    init(arguments: [String]) throws {
        var values: [String: String] = [:]
        let allowed: Set<String> = ["--asset-root", "--process-generation", "--build-identity", "--expected-client-build", "--schema-directory", "--synthetic-pcm", "--synthetic-chunk-frames", "--synthetic-total-frames", "--synthetic-seal-barrier-directory", "--synthetic-start-barrier-directory"]
        guard arguments.count % 2 == 0 else { throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Helper flags require explicit values") }
        for index in stride(from: 0, to: arguments.count, by: 2) {
            let key = arguments[index]
            guard allowed.contains(key), values[key] == nil else { throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Unknown or repeated helper option") }
            values[key] = arguments[index + 1]
        }
        guard let root = values["--asset-root"], let generation = values["--process-generation"],
              let buildPath = values["--build-identity"], let clientPath = values["--expected-client-build"],
              let schemas = values["--schema-directory"], generation.range(of: "^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$", options: .regularExpression) != nil else {
            throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Helper requires asset root, generation, schemas and exact build identities")
        }
        self.assetRoot = URL(fileURLWithPath: root, isDirectory: true)
        self.generation = generation
        self.build = try RecordingJSON.parse(RecordingPrivateFile.read(buildPath))
        self.expectedClientBuild = try RecordingJSON.parse(RecordingPrivateFile.read(clientPath))
        self.schemaDirectory = URL(fileURLWithPath: schemas, isDirectory: true)
        self.syntheticChunkFrames = Int(values["--synthetic-chunk-frames"] ?? "480") ?? 0
        guard (1...4096).contains(syntheticChunkFrames) else { throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Synthetic callback frame count is out of bounds") }
        if let raw = values["--synthetic-total-frames"] {
            guard values["--synthetic-pcm"] != nil, let frames = UInt64(raw), (1...28_800_000).contains(frames) else {
                throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Synthetic total frames require explicit PCM and a 1...28800000 bound")
            }
            self.syntheticTotalFrames = frames
        } else { self.syntheticTotalFrames = nil }
        if let path = values["--synthetic-pcm"] {
            let data = try RecordingPrivateFile.read(path)
            guard !data.isEmpty, data.count <= 1_048_576, data.count % 3 == 0 else {
                throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Synthetic PCM must be a bounded nonempty mono24-bit fixture")
            }
            self.syntheticPCM = data
        } else { self.syntheticPCM = nil }
        syntheticSealBarrier = values["--synthetic-seal-barrier-directory"]
        guard syntheticSealBarrier == nil || syntheticPCM != nil else {
            throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Seal barrier requires explicit synthetic PCM")
        }
        // Holds the first synthetic start before any chunk, so Stop can land at zero frames.
        syntheticStartBarrier = values["--synthetic-start-barrier-directory"]
        guard syntheticStartBarrier == nil || syntheticPCM != nil else {
            throw NativeCaptureFailure("RECORDING.INVALID_MESSAGE", "Start barrier requires explicit synthetic PCM")
        }
        let executable = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        let digest = SHA256.hash(data: try Data(contentsOf: executable, options: .mappedIfSafe)).map { String(format: "%02x", $0) }.joined()
        guard build.object?["executable_sha256"] == .string(digest) else {
            throw NativeCaptureFailure("RECORDING.BUILD_MISMATCH", "Helper executable differs from its configured immutable build")
        }
    }
}
