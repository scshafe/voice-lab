import Foundation
import RecordingContracts
@testable import RecordingHarness
import Testing

/// Synthetic reservations only. Storage sharing is proven by prompt buffer identity, never
/// inferred from process footprint, and every retained record must encode byte for byte.
@Suite(.serialized)
struct RecordingBenchmarkReservationInternerTests {
    @Test func identicalIndependentlyParsedCatalogsShareOneRetainedCopy() throws {
        let first = try reservation()
        let second = try reservation()
        #expect(promptBuffer(first) != nil && promptBuffer(first) != promptBuffer(second))
        #expect(promptCount(second) == 1_102)
        var interner = RecordingBenchmarkReservationInterner()
        let retainedFirst = interner.intern(first)
        let retainedSecond = interner.intern(second)
        #expect(promptBuffer(retainedFirst) == promptBuffer(first))
        #expect(promptBuffer(retainedSecond) == promptBuffer(first))
        #expect(try encoded(retainedSecond) == encoded(second))
        #expect(RecordingBenchmarkReservationInterner.identical(.object(retainedSecond), .object(second)))
        // Per-take fields stay the take's own; only the group is shared.
        let third = try reservation(takeID: "take-3")
        let retainedThird = interner.intern(third)
        #expect(promptBuffer(retainedThird) == promptBuffer(first))
        #expect(retainedThird["take_id"] == .string("take-3"))
        #expect(try encoded(retainedThird) == encoded(third))
    }

    @Test func changedGroupWithIdenticalCatalogSharesOnlyTheCatalog() throws {
        var interner = RecordingBenchmarkReservationInterner()
        let first = try reservation()
        _ = interner.intern(first)
        let reopened = try reservation(openedAt: "2026-09-14T01:00:00Z")
        let retained = interner.intern(reopened)
        #expect(promptBuffer(retained) == promptBuffer(first))
        #expect(retained["group"]?.object?["opened_at"] == .string("2026-09-14T01:00:00Z"))
        #expect(try encoded(retained) == encoded(reopened))
        // The reopened group is now the retained one.
        let again = try reservation(openedAt: "2026-09-14T01:00:00Z")
        #expect(promptBuffer(interner.intern(again)) == promptBuffer(first))
    }

    @Test func catalogsThatDifferInAnyByteAreNeverShared() throws {
        // JSON escapes keep the composed and decomposed spellings visible in this source.
        let variants: [(String, [String: RecordingJSON], [String: RecordingJSON])] = try [
            ("one prompt", reservation(), reservation(prompts: 1_101)),
            ("integer versus number", reservation(seconds: "1"), reservation(seconds: "1.0")),
            ("NFC versus NFD text", reservation(text: #"caf\u00e9"#), reservation(text: #"cafe\u0301"#)),
            ("negative versus positive zero", reservation(seconds: "-0.0"), reservation(seconds: "0.0")),
        ]
        for (name, retainedFirst, candidate) in variants {
            var interner = RecordingBenchmarkReservationInterner()
            _ = interner.intern(retainedFirst)
            let retained = interner.intern(candidate)
            #expect(promptBuffer(retained) == promptBuffer(candidate), "\(name)")
            #expect(promptBuffer(retained) != promptBuffer(retainedFirst), "\(name)")
            #expect(try encoded(retained) == encoded(candidate), "\(name)")
            #expect(try encoded(retained) != encoded(retainedFirst), "\(name)")
        }
        // The Unicode and zero variants are exactly the ones synthesized equality merges.
        let composed = try reservation(text: #"caf\u00e9"#), decomposed = try reservation(text: #"cafe\u0301"#)
        #expect(RecordingJSON.object(composed) == .object(decomposed))
        let negative = try reservation(seconds: "-0.0"), positive = try reservation(seconds: "0.0")
        #expect(RecordingJSON.object(negative) == .object(positive))
        #expect(try firstSecondsToken(reservation(seconds: "1")) == .integer(1))
        #expect(try firstSecondsToken(reservation(seconds: "1.0")) == .number(1.0))
        #expect(firstSeconds(negative)?.bitPattern != firstSeconds(positive)?.bitPattern)
    }

    @Test func exactIdentityComparesKeysAndValuesAsStoredBytes() {
        typealias Interner = RecordingBenchmarkReservationInterner
        let composedKey: RecordingJSON = .object(["caf\u{E9}": .integer(1)])
        let decomposedKey: RecordingJSON = .object(["cafe\u{301}": .integer(1)])
        #expect(composedKey == decomposedKey && !Interner.identical(composedKey, decomposedKey))
        #expect(RecordingJSON.number(-0.0) == .number(0.0) && !Interner.identical(.number(-0.0), .number(0.0)))
        #expect(!Interner.identical(.integer(1), .number(1.0)))
        #expect(!Interner.identical(.array([.null]), .array([.null, .null])))
        #expect(!Interner.identical(.object(["a": .null]), .object(["a": .null, "b": .null])))
        #expect(Interner.identical(.object(["caf\u{E9}": .array([.number(-0.0), .boolean(true), .string("x")])]),
                                   .object(["caf\u{E9}": .array([.number(-0.0), .boolean(true), .string("x")])])))
    }

    @Test func reservationWithoutGroupIsRetainedUnchanged() throws {
        var interner = RecordingBenchmarkReservationInterner()
        var bare = try reservation()
        bare["group"] = nil
        #expect(try encoded(interner.intern(bare)) == encoded(bare))
    }

    private func reservation(prompts: Int = 1_102, takeID: String = "take-1",
                             openedAt: String = "2026-09-14T00:00:00Z",
                             text: String = "Synthetic prompt", seconds: String = "4.5") throws -> [String: RecordingJSON] {
        let catalog = (0..<prompts).map { index -> String in
            #"{"prompt_id":"prompt-\#(index)","text":"\#(text) \#(index)","category":"neutral","#
                + #""style":"plain","expected_seconds":\#(index == 0 ? seconds : "4.5"),"holdout":false,"#
                + #""reference_candidate":false}"#
        }.joined(separator: ",")
        let json: String = #"{"schema_version":"cole-voice-lab.capture-reservation/v1","take_id":"\#(takeID)","#
            + #""operation_id":"record-1","original_path":"takes/\#(takeID).wav","partial_path":"takes/\#(takeID).partial","#
            + #""process_generation":"generation","requested_at":"2026-09-14T00:00:01Z","maximum_frames":28800000,"#
            + #""prompt":{"prompt_id":"prompt-0"},"group":{"schema_version":"cole-voice-lab.recording-group/v1","#
            + #""group_id":"group-1","context_revision":1,"subject_id":"subject","catalog_sha256":"\#(String(repeating: "a", count: 64))","#
            + #""capture_device":{"device_id":"synthetic","display_name":"Synthetic"},"capture_host":"host","#
            + #""opened_at":"\#(openedAt)","reason":"app-start","gate":{"state":"not-required"},"#
            + #""catalog":{"schema_version":"cole-voice-lab.prompt-suite/v1","suite_id":"suite","purpose":"capture-corpus","#
            + #""prompts":[\#(catalog)]}}}"#
        return try #require(RecordingJSON.parse(Data(json.utf8)).object)
    }

    private func prompts(_ reservation: [String: RecordingJSON]) -> [RecordingJSON]? {
        reservation["group"]?.object?["catalog"]?.object?["prompts"]?.array
    }

    private func promptBuffer(_ reservation: [String: RecordingJSON]) -> Int? {
        prompts(reservation)?.withUnsafeBufferPointer { $0.baseAddress.map { Int(bitPattern: $0) } }
    }

    private func promptCount(_ reservation: [String: RecordingJSON]) -> Int { prompts(reservation)?.count ?? 0 }

    private func firstSecondsToken(_ reservation: [String: RecordingJSON]) -> RecordingJSON? {
        prompts(reservation)?.first?.object?["expected_seconds"]
    }

    private func firstSeconds(_ reservation: [String: RecordingJSON]) -> Double? {
        if case .number(let value)? = firstSecondsToken(reservation) { return value }
        return nil
    }

    private func encoded(_ reservation: [String: RecordingJSON]) throws -> Data {
        try RecordingJSON.object(reservation).transportData()
    }
}
