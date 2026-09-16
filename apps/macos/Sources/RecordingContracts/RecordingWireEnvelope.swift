import Foundation

/// Correlation metadata shared by recorder, service and telemetry transports.
public struct RecordingMessageMeta: Codable, Equatable, Sendable {
    public let protocol_version: String
    public let request_id: String
    public let process_generation: String
    public let state_revision: Int64
    public let event_sequence: Int64
    public let take_id: String?
}

/// Typed envelope is constructed only after strict schema/semantic validation.
/// Codable is a transport decoder, never the unknown-field validation boundary.
public struct RecordingWireEnvelope: Sendable {
    public let data: RecordingJSON
    public let error: RecordingJSON
    public let meta: RecordingMessageMeta

    public static func decode(_ payload: Data, schema: Data) throws -> RecordingWireEnvelope {
        try decode(payload, validator: RecordingSchemaValidator(schema: schema))
    }

    public static func decode(_ payload: Data, validator: RecordingSchemaValidator) throws -> RecordingWireEnvelope {
        let value = try validator.validate(payload)
        guard let object = value.object, let metadata = object["meta"] else {
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Envelope requires correlation metadata")
        }
        let meta = try JSONDecoder().decode(RecordingMessageMeta.self, from: JSONEncoder().encode(metadata))
        return RecordingWireEnvelope(data: object["data"] ?? .null, error: object["error"] ?? .null, meta: meta)
    }
}
