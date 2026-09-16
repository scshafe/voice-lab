import Foundation
@testable import RecordingContracts
import Testing

private final class PatternCompilations: @unchecked Sendable {
    private let lock = NSLock()
    private var patterns: [String] = []
    func record(_ pattern: String) { lock.lock(); defer { lock.unlock() }; patterns.append(pattern) }
    var recorded: [String] { lock.lock(); defer { lock.unlock() }; return patterns }
}

@Suite struct RecordingPreparedSchemaTests {
    private func bytes(_ value: String) -> Data { Data(value.utf8) }

    @Test func uniquePatternsCompileOnceAcrossReferencesArraysAndRepeatedValidation() throws {
        let schema = bytes(##"{"$defs":{"token":{"type":"string","pattern":"^[a-z]+$"}},"type":"object","properties":{"left":{"$ref":"#/$defs/token"},"right":{"type":"array","items":{"type":"string","pattern":"^[a-z]+$"}},"extra":{"type":"object","additionalProperties":{"type":"string","pattern":"^[0-9]+$"}}},"required":["left","right","extra"],"additionalProperties":false}"##)
        let calls = PatternCompilations()
        let prepared = try RecordingSchemaValidator(schema: schema, compilePattern: {
            calls.record($0); return try NSRegularExpression(pattern: $0)
        })
        let ordinary = try RecordingSchemaValidator(schema: schema)
        let payload = bytes(#"{"left":"one","right":["two","three"],"extra":{"key":"123"}}"#)
        for _ in 0..<20 { #expect(try prepared.validate(payload) == ordinary.validate(payload)) }
        #expect(calls.recorded.sorted() == ["^[0-9]+$", "^[a-z]+$"])
        #expect(throws: RecordingContractError.self) { try prepared.validate(bytes(#"{"left":"1","right":[],"extra":{}}"#)) }
    }

    @Test func validationUsesTheRetainedCompiledExpression() throws {
        let schema = bytes(#"{"type":"string","pattern":"^[a-z]+$"}"#)
        // An injected compiler makes cache use observable without timing assertions.
        let prepared = try RecordingSchemaValidator(schema: schema, compilePattern: { _ in
            try NSRegularExpression(pattern: "^approved$")
        })
        #expect(try prepared.validate(bytes(#""approved""#)) == .string("approved"))
        #expect(throws: RecordingContractError.self) { try prepared.validate(bytes(#""different""#)) }
    }

    @Test func preparedAndOrdinaryPreserveDefaultsAndUnionMatchCounts() throws {
        let schema = bytes(#"{"type":"object","properties":{"choice":{"anyOf":[{"type":"null"},{"type":"object","properties":{"token":{"type":"string","pattern":"^take-[0-9]+$","default":"take-1"}},"required":["token"],"additionalProperties":false}]},"mode":{"type":"string","default":"draft"}},"required":["choice"],"additionalProperties":false}"#)
        let payload = bytes(#"{"choice":{}}"#)
        let expected: RecordingJSON = .object(["choice": .object(["token": .string("take-1")]), "mode": .string("draft")])
        for validator in [try RecordingSchemaValidator(schema: schema), try RecordingSchemaValidator(preparing: schema)] {
            #expect(try validator.validate(payload) == expected)
            #expect(throws: RecordingContractError.self) { try validator.validate(bytes(#"{"choice":{"token":"wrong"}}"#)) }
            #expect(throws: RecordingContractError.self) { try validator.validate(bytes(#"{"choice":null,"unknown":true}"#)) }
        }
        let overlap = bytes(#"{"oneOf":[{"type":"string","pattern":"^a"},{"type":"string","pattern":"a$"}]}"#)
        let prepared = try RecordingSchemaValidator(preparing: overlap)
        #expect(try prepared.validate(bytes(#""ab""#)) == .string("ab"))
        #expect(throws: RecordingContractError.self) { try prepared.validate(bytes(#""a""#)) }
    }

    @Test func invalidUnusedPatternDoesNotFailPreparationOrAnEarlierUnionBranch() throws {
        let schema = bytes(#"{"anyOf":[{"type":"null"},{"type":"string","pattern":"["}]}"#)
        let calls = PatternCompilations()
        let prepared = try RecordingSchemaValidator(schema: schema, compilePattern: {
            calls.record($0); return try NSRegularExpression(pattern: $0)
        })
        let ordinary = try RecordingSchemaValidator(schema: schema)
        #expect(try prepared.validate(bytes("null")) == ordinary.validate(bytes("null")))
        for _ in 0..<2 {
            #expect(throws: (any Error).self) { try prepared.validate(bytes(#""text""#)) }
            #expect(throws: (any Error).self) { try ordinary.validate(bytes(#""text""#)) }
        }
        #expect(calls.recorded == ["["])
    }

    @Test func defaultsContainingPatternDataAreNotCompiledAsSchemas() throws {
        let calls = PatternCompilations()
        let schema = bytes(#"{"type":"object","properties":{"data":{"type":"object","default":{"pattern":"["}}}}"#)
        let prepared = try RecordingSchemaValidator(schema: schema, compilePattern: {
            calls.record($0); return try NSRegularExpression(pattern: $0)
        })
        #expect(try prepared.validate(bytes("{}")) == .object(["data": .object(["pattern": .string("[")])]))
        #expect(calls.recorded.isEmpty)
    }

    @Test func preparationPreservesSemanticPathDateTypeAndKeywordRejections() throws {
        let schema = bytes(#"{"type":"object","title":"Prompt","properties":{"text":{"type":"string"},"style":{"type":"string"},"source_path":{"type":"string"},"date":{"type":"string","format":"date-time"},"count":{"type":"integer","minimum":1}},"required":["text","style","source_path","date","count"],"additionalProperties":false}"#)
        let valid: RecordingJSON = .object(["text": .string("Read this"), "style": .string("neutral"), "source_path": .string("tmp/take.wav"), "date": .string("2026-09-13T00:00:00Z"), "count": .integer(1)])
        for validator in [try RecordingSchemaValidator(schema: schema), try RecordingSchemaValidator(preparing: schema)] {
            #expect(try validator.validate(valid.transportData()) == valid)
            for (key, value) in [("text", RecordingJSON.string(" ")), ("source_path", .string("../take.wav")), ("date", .string("2026-09-13T00:00:00")), ("count", .number(1.0)), ("count", .integer(0)), ("unknown", .boolean(true))] {
                var object = try #require(valid.object); object[key] = value
                #expect(throws: RecordingContractError.self) { try validator.validate(RecordingJSON.object(object).transportData()) }
            }
        }
        let unsupported = try RecordingSchemaValidator(preparing: bytes(#"{"type":"string","unsupported":true}"#))
        #expect(throws: RecordingContractError.self) { try unsupported.validate(bytes(#""value""#)) }
    }

    @Test func preparedEnvelopeDecoderRetainsMetadataAndUsesItsValidator() throws {
        let schema = bytes(#"{"type":"object","properties":{"data":{"type":"string","pattern":"^[a-z]+$"}}}"#)
        let validator = try RecordingSchemaValidator(schema: schema, compilePattern: { _ in
            try NSRegularExpression(pattern: "^approved$")
        })
        var envelope: [String: RecordingJSON] = ["data": .string("approved"), "error": .null,
            "meta": .object(["protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("request-1"), "process_generation": .string("generation"), "state_revision": .integer(3), "event_sequence": .integer(4), "take_id": .null])]
        for _ in 0..<3 {
            let payload = try RecordingJSON.object(envelope).transportData()
            let result = try RecordingWireEnvelope.decode(payload, validator: validator)
            let ordinary = try RecordingWireEnvelope.decode(payload, schema: schema)
            #expect(result.meta == ordinary.meta && result.data == ordinary.data && result.error == ordinary.error)
        }
        envelope["data"] = .string("different")
        #expect(throws: RecordingContractError.self) {
            try RecordingWireEnvelope.decode(RecordingJSON.object(envelope).transportData(), validator: validator)
        }
    }
}
