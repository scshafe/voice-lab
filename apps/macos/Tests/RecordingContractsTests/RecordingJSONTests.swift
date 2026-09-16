import Foundation
import RecordingContracts
import Testing

private enum RecordingJSONFixtures {
    static let integer = Data("9223372036854775807".utf8)
    static let decimalInteger = Data("1.0".utf8)
    static let exponentInteger = Data("1e0".utf8)
    static let boolean = Data("true".utf8)
    static let unsignedMaximum = Data("18446744073709551615".utf8)
    static let nonfinite = ["NaN", "Infinity", "-Infinity", "1e999"].map { Data($0.utf8) }
}

@Suite struct RecordingJSONTests {
    @Test func testMaximumSignedIntegerRetainsExactValue() throws {
        #expect(try RecordingJSON.parse(RecordingJSONFixtures.integer) == .integer(Int64.max))
    }

    @Test func testFloatingPointTokenDoesNotBecomeStrictInteger() throws {
        #expect(try RecordingJSON.parse(RecordingJSONFixtures.decimalInteger) == .number(1.0))
        #expect(try RecordingJSON.parse(RecordingJSONFixtures.exponentInteger) == .number(1.0))
    }

    @Test func testBooleanTokenDoesNotBecomeNumber() throws {
        #expect(try RecordingJSON.parse(RecordingJSONFixtures.boolean) == .boolean(true))
    }

    @Test func testFloatingPointTransportSerializationPreservesTokenKind() throws {
        let value = try RecordingJSON.parse(RecordingJSONFixtures.decimalInteger)
        #expect(try RecordingJSON.parse(value.transportData()) == value)
    }

    @Test func testUnsignedIntegerCannotWrapIntoSignedRange() {
        #expect(throws: RecordingContractError.self) {
            try RecordingJSON.parse(RecordingJSONFixtures.unsignedMaximum)
        }
    }

    @Test func testNonfiniteJSONTokensFailParsing() {
        for payload in RecordingJSONFixtures.nonfinite {
            #expect(throws: (any Error).self) { try RecordingJSON.parse(payload) }
        }
    }
}
