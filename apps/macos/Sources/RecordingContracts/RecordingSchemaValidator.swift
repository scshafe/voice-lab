import Foundation

/// Strict validator for the JSON-schema vocabulary emitted by Python contracts.
/// Unknown validation keywords fail closed; schema changes require conformance.
public struct RecordingSchemaValidator: Sendable {
    private let root: RecordingJSON
    private let preparedPatterns: [String: Result<NSRegularExpression, any Error>]?

    public init(schema: Data) throws {
        root = try RecordingJSON.parse(schema)
        preparedPatterns = nil
    }

    /// Retain only patterns declared by this immutable schema. One-off callers
    /// keep their existing lazy path; process channels prepare once before launch.
    public init(preparing schema: Data) throws {
        try self.init(schema: schema, compilePattern: { try NSRegularExpression(pattern: $0) })
    }

    init(schema: Data, compilePattern: @Sendable (String) throws -> NSRegularExpression) throws {
        root = try RecordingJSON.parse(schema)
        var patterns: [String: Result<NSRegularExpression, any Error>] = [:]
        Self.preparePatterns(root, into: &patterns, compile: compilePattern)
        preparedPatterns = patterns
    }

    private static func preparePatterns(
        _ schema: RecordingJSON, into patterns: inout [String: Result<NSRegularExpression, any Error>],
        compile: @Sendable (String) throws -> NSRegularExpression
    ) {
        guard let fields = schema.object else { return }
        if let pattern = fields["pattern"]?.string, patterns[pattern] == nil {
            // Invalid unused branches did not previously fail initialization.
            // Preserve that boundary: rethrow the retained error only on use.
            patterns[pattern] = Result { try compile(pattern) }
        }
        for key in ["$defs", "properties"] {
            for child in fields[key]?.object?.values ?? [:].values {
                preparePatterns(child, into: &patterns, compile: compile)
            }
        }
        for key in ["items", "additionalProperties"] {
            if let child = fields[key] { preparePatterns(child, into: &patterns, compile: compile) }
        }
        for key in ["anyOf", "oneOf"] {
            for child in fields[key]?.array ?? [] { preparePatterns(child, into: &patterns, compile: compile) }
        }
    }

    /// Validate shape and transport semantics without writing canonical evidence.
    public func validate(_ data: Data) throws -> RecordingJSON {
        guard !data.isEmpty, data.count <= RecordingFrameCodec.controlMaximumBytes else {
            throw RecordingContractError(code: "RECORDING.FRAME_SIZE", message: "JSON contract payload exceeds its bound")
        }
        let raw = try RecordingJSON.parse(data)
        let value = try normalized(raw, schema: root)
        try validate(value, schema: root, path: "$")
        return value
    }

    /// Apply only declared defaults so omitted optional transport fields have the
    /// same semantic meaning as Python models. This never serializes evidence.
    private func normalized(_ value: RecordingJSON, schema: RecordingJSON) throws -> RecordingJSON {
        guard let fields = schema.object else { return value }
        if let reference = fields["$ref"]?.string, let name = reference.split(separator: "/").last,
           let resolved = root.object?["$defs"]?.object?[String(name)] {
            return try normalized(value, schema: resolved)
        }
        if let variants = (fields["oneOf"] ?? fields["anyOf"])?.array {
            for variant in variants {
                let candidate = try normalized(value, schema: variant)
                do { try validate(candidate, schema: variant, path: "$"); return candidate }
                catch is RecordingContractError { continue }
            }
            return value
        }
        if case .object(var object) = value, let properties = fields["properties"]?.object {
            for (key, property) in properties {
                if let item = object[key] { object[key] = try normalized(item, schema: property) }
                else if let defaultValue = property.object?["default"] { object[key] = defaultValue }
            }
            return .object(object)
        }
        if case .array(let items) = value, let itemSchema = fields["items"] {
            return .array(try items.map { try normalized($0, schema: itemSchema) })
        }
        return value
    }

    private func validate(_ value: RecordingJSON, schema: RecordingJSON, path: String) throws {
        guard let fields = schema.object else { throw failure(path, "Schema is not an object") }
        let supported: Set<String> = ["$defs", "$ref", "title", "description", "default", "type", "properties", "required", "additionalProperties", "items", "minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "const", "enum", "anyOf", "oneOf", "discriminator", "pattern", "format"]
        guard Set(fields.keys).isSubset(of: supported) else {
            throw failure(path, "Generated schema contains an unsupported validation keyword")
        }
        if let reference = fields["$ref"]?.string {
            let pieces = reference.split(separator: "/").map(String.init)
            guard pieces.count == 3, pieces[0] == "#", pieces[1] == "$defs",
                  let resolved = root.object?["$defs"]?.object?[pieces[2]] else {
                throw failure(path, "Generated schema reference cannot be resolved")
            }
            try validate(value, schema: resolved, path: path)
            return
        }
        for union in ["anyOf", "oneOf"] {
            if let variants = fields[union]?.array {
                var matchCount = 0
                for variant in variants {
                    do { try validate(value, schema: variant, path: path); matchCount += 1 }
                    catch is RecordingContractError { continue }
                }
                guard (union == "anyOf" && matchCount > 0) || (union == "oneOf" && matchCount == 1) else {
                    throw failure(path, "Payload does not match its declared union")
                }
            }
        }
        if let constant = fields["const"], value != constant { throw failure(path, "Literal value mismatch") }
        if let values = fields["enum"]?.array, !values.contains(value) { throw failure(path, "Unknown enum value") }
        if let type = fields["type"]?.string {
            switch (type, value) {
            case ("object", .object(let object)):
                let properties = fields["properties"]?.object ?? [:]
                let required = Set((fields["required"]?.array ?? []).compactMap(\.string))
                guard required.isSubset(of: Set(object.keys)) else { throw failure(path, "Required field missing") }
                for (name, item) in object {
                    if let property = properties[name] { try validate(item, schema: property, path: path + "." + name) }
                    else if fields["additionalProperties"] == .boolean(false) { throw failure(path, "Unknown object field") }
                    else if let additional = fields["additionalProperties"], additional.object != nil {
                        try validate(item, schema: additional, path: path + "." + name)
                    }
                }
                if let title = fields["title"]?.string {
                    try RecordingSemanticValidator.validate(title: title, object: object)
                }
            case ("array", .array(let items)):
                try count(items.count, minimum: fields["minItems"]?.integer, maximum: fields["maxItems"]?.integer, path: path)
                if let itemSchema = fields["items"] {
                    for (index, item) in items.enumerated() { try validate(item, schema: itemSchema, path: "\(path)[\(index)]") }
                }
            case ("string", .string(let text)):
                try count(text.unicodeScalars.count, minimum: fields["minLength"]?.integer, maximum: fields["maxLength"]?.integer, path: path)
                if let pattern = fields["pattern"]?.string {
                    let regex: NSRegularExpression
                    if let preparedPatterns {
                        guard let prepared = preparedPatterns[pattern] else {
                            throw failure(path, "Prepared schema pattern is missing")
                        }
                        regex = try prepared.get()
                    } else {
                        regex = try NSRegularExpression(pattern: pattern)
                    }
                    guard regex.firstMatch(in: text, range: NSRange(text.startIndex..., in: text)) != nil else { throw failure(path, "String does not match its pattern") }
                }
                if fields["format"]?.string == "date-time", RecordingSemanticValidator.timestamp(text) == nil { throw failure(path, "Timestamp requires an explicit timezone") }
                if path.hasSuffix("_path") || path.hasSuffix(".partial_path") || path.hasSuffix(".original_path") {
                    try RecordingSemanticValidator.relativePath(text)
                }
            case ("integer", .integer), ("number", .integer), ("number", .number):
                let number = value.number!
                for key in ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"] {
                    if let limit = fields[key]?.number {
                        let valid = switch key {
                        case "minimum": number >= limit
                        case "maximum": number <= limit
                        case "exclusiveMinimum": number > limit
                        default: number < limit
                        }
                        guard valid else { throw failure(path, "Numeric value outside declared bounds") }
                    }
                }
            case ("boolean", .boolean), ("null", .null): break
            default: throw failure(path, "Strict JSON type mismatch")
            }
        }
    }

    private func count(_ value: Int, minimum: Int64?, maximum: Int64?, path: String) throws {
        if let minimum, value < minimum { throw failure(path, "Value shorter than declared minimum") }
        if let maximum, value > maximum { throw failure(path, "Value longer than declared maximum") }
    }

    private func failure(_ path: String, _ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: message, context: ["field": path])
    }
}
