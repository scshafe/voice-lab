import Foundation

/// Bounded observable state-transition evidence; telemetry never fills this history.
public struct RecordingWorkspaceTransition: Codable, Sendable {
    public let monotonicNanoseconds: UInt64
    public let phase: String
    public let promptID: String?
    public let promptText: String?
    public let takeID: String?
    public let operationID: String?
    public let status: String
}
