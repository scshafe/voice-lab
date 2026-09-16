import Foundation

/// Machine-readable local contract failure; no private payload is logged.
public struct RecordingContractError: Error, CustomStringConvertible, Sendable {
    public let code: String
    public let message: String
    public let context: [String: String]

    public init(code: String, message: String, context: [String: String] = [:]) {
        self.code = code
        self.message = message
        self.context = context
    }

    public var description: String { "\(code): \(message)" }
}
