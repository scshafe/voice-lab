import Foundation
@testable import RecordingContracts
import Testing

@Suite struct RecordingTimestampTests {
    @Test func testMicrosecondOrderingDoesNotCollapseIntoMilliseconds() throws {
        let earlier = try #require(RecordingSemanticValidator.timestamp("2026-09-11T00:00:00.123455Z"))
        let later = try #require(RecordingSemanticValidator.timestamp("2026-09-11T00:00:00.123456Z"))
        #expect(earlier < later)
        #expect(RecordingTimestamp.string(later) == "2026-09-11T00:00:00.123456Z")
    }

    @Test func testUTCOffsetNormalizationKeepsMicroseconds() throws {
        let date = try #require(RecordingTimestamp.parse("2026-09-10T17:00:00.123456-07:00"))
        #expect(RecordingTimestamp.string(date) == "2026-09-11T00:00:00.123456Z")
    }
}
