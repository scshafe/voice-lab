import Foundation
import CoreFoundation

/// Lossless JSON shape used for Python-owned generated schema validation.
public enum RecordingJSON: Encodable, Equatable, Sendable {
    case object([String: RecordingJSON])
    case array([RecordingJSON])
    case string(String)
    case integer(Int64)
    case number(Double)
    case boolean(Bool)
    case null

    /// Preserve integer versus floating-point JSON tokens for strict validation.
    /// JSONDecoder.decode(Int64.self) accepts 1.0, which Python strict int rejects.
    public static func parse(_ data: Data) throws -> RecordingJSON {
        func convert(_ value: Any) throws -> RecordingJSON {
            if value is NSNull { return .null }
            if let number = value as? NSNumber {
                if CFGetTypeID(number) == CFBooleanGetTypeID() { return .boolean(number.boolValue) }
                if ["d", "f"].contains(String(cString: number.objCType)) {
                    guard number.doubleValue.isFinite else { throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "JSON number must be finite") }
                    return .number(number.doubleValue)
                }
                guard let integer = Int64(number.stringValue) else {
                    throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "JSON integer exceeds signed 64-bit transport range")
                }
                return .integer(integer)
            }
            if let text = value as? String { return .string(text) }
            if let array = value as? [Any] { return .array(try array.map(convert)) }
            if let object = value as? [String: Any] { return .object(try object.mapValues(convert)) }
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Unsupported JSON value")
        }
        return try convert(JSONSerialization.jsonObject(with: data, options: [.fragmentsAllowed]))
    }

    /// Transport JSON preserves numeric token kinds; Python owns canonical bytes.
    public func transportData() throws -> Data {
        func text(_ value: RecordingJSON) throws -> String {
            switch value {
            case .object(let object):
                return "{" + (try object.keys.sorted().map { key in
                    try text(.string(key)) + ":" + text(object[key]!)
                }).joined(separator: ",") + "}"
            case .array(let values): return "[" + (try values.map(text)).joined(separator: ",") + "]"
            case .string(let value): return String(decoding: try JSONEncoder().encode(value), as: UTF8.self)
            case .integer(let value): return String(value)
            case .number(let value):
                guard value.isFinite else { throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "JSON number must be finite") }
                return String(value)
            case .boolean(let value): return value ? "true" : "false"
            case .null: return "null"
            }
        }
        return Data(try text(self).utf8)
    }

    public func encode(to encoder: Encoder) throws {
        var value = encoder.singleValueContainer()
        switch self {
        case .object(let item): try value.encode(item)
        case .array(let item): try value.encode(item)
        case .string(let item): try value.encode(item)
        case .integer(let item): try value.encode(item)
        case .number(let item): try value.encode(item)
        case .boolean(let item): try value.encode(item)
        case .null: try value.encodeNil()
        }
    }

    public var object: [String: RecordingJSON]? {
        if case .object(let value) = self { return value }
        return nil
    }

    public var string: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    public var integer: Int64? {
        if case .integer(let value) = self { return value }
        return nil
    }

    public var array: [RecordingJSON]? {
        if case .array(let value) = self { return value }
        return nil
    }

    public var number: Double? {
        switch self {
        case .integer(let value): Double(value)
        case .number(let value): value
        default: nil
        }
    }
}
