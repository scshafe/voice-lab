import CryptoKit
import Foundation
import RecordingContracts

/// Exact launch and schema identities for one inherited local process channel.
public struct RecordingProcessConfiguration: Sendable {
    public enum Peer: String, Sendable { case recorder, service }
    public let executable: URL
    public let arguments: [String]
    public let environment: [String: String]?
    public let schemaDirectory: URL
    public let processGeneration: String
    public let clientBuild: RecordingJSON
    public let expectedPeerBuild: RecordingJSON
    public let peer: Peer

    public init(executable: URL, arguments: [String], environment: [String: String]? = nil,
                schemaDirectory: URL, processGeneration: String, clientBuild: RecordingJSON,
                expectedPeerBuild: RecordingJSON, peer: Peer) {
        self.executable = executable
        self.arguments = arguments
        self.environment = environment
        self.schemaDirectory = schemaDirectory
        self.processGeneration = processGeneration
        self.clientBuild = clientBuild
        self.expectedPeerBuild = expectedPeerBuild
        self.peer = peer
    }

    /// Check the executable selected for launch before opening process channels.
    func verifyExecutable() throws {
        let url = executable.resolvingSymlinksInPath()
        let data = try Data(contentsOf: url, options: .mappedIfSafe)
        let digest = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        guard expectedPeerBuild.object?["executable_sha256"] == .string(digest) else {
            throw RecordingContractError(code: "RECORDING.BUILD_MISMATCH", message: "Process executable does not match its expected build identity")
        }
    }
}
