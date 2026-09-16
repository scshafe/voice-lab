import Foundation
import RecordingContracts

/// Explicit request IDs replay byte-identical envelopes for this process session.
/// The finite cache fails closed when full; durable operation IDs survive reconnect.
struct RecordingRequestReplayCache {
    private struct Entry { let command: RecordingJSON; let takeID: String?; let payload: Data }
    private var entries: [String: Entry] = [:]
    let maximumEntries: Int

    init(maximumEntries: Int = 128) { self.maximumEntries = maximumEntries }

    mutating func payload(requestID: String?, command: RecordingJSON, takeID: String?, envelope: RecordingJSON) throws -> Data {
        guard let requestID else { return try envelope.transportData() }
        if let previous = entries[requestID] {
            guard previous.command == command, previous.takeID == takeID else {
                throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Request ID cannot be reused with different command or take identity")
            }
            return previous.payload
        }
        guard entries.count < maximumEntries else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Explicit request replay cache is full; reconnect using durable operation IDs")
        }
        let payload = try envelope.transportData()
        entries[requestID] = Entry(command: command, takeID: takeID, payload: payload)
        return payload
    }
}
