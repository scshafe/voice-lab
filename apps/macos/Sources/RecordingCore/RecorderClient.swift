import Foundation
import RecordingContracts

/// UI control surface for the persistent recorder; Stop goes straight to helper.
public final class RecorderClient: Sendable {
    public let channel: RecordingProcessChannel

    public init(configuration: RecordingProcessConfiguration) {
        precondition(configuration.peer == .recorder)
        channel = RecordingProcessChannel(configuration: configuration)
    }

    public func connect() async throws { try await channel.start() }

    public func start(reservation: RecordingJSON, reservationSHA256: String,
                      requestID: String? = nil) async throws -> RecordingWireEnvelope {
        guard let takeID = reservation.object?["take_id"]?.string else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Start requires a reserved take identity")
        }
        return try await channel.request(.object([
            "command": .string("start"), "reservation": reservation,
            "reservation_sha256": .string(reservationSHA256),
        ]), takeID: takeID, requestID: requestID)
    }

    public func stop(takeID: String, operationID: String, requestID: String? = nil) async throws -> RecordingWireEnvelope {
        try await channel.request(.object([
            "command": .string("stop"), "take_id": .string(takeID),
            "operation_id": .string(operationID),
        ]), takeID: takeID, requestID: requestID)
    }

    public func snapshot() async throws -> RecordingWireEnvelope { try await channel.requestSnapshot() }

    public func shutdown() async throws -> RecordingWireEnvelope {
        try await channel.request(.object(["command": .string("shutdown")]), takeID: nil)
    }
}
