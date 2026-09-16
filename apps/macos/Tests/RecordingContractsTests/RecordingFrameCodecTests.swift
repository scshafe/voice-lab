import Foundation
import RecordingContracts
import Testing

@Suite struct RecordingFrameCodecTests {
    @Test func testSplitHeaderAndBodyYieldOnlyCompleteFrame() throws {
        let payload = Data("{\"synthetic\":true}".utf8)
        let encoded = try RecordingFrameCodec.encode(payload)
        var codec = RecordingFrameCodec()
        var received: [Data] = []
        for byte in encoded { received.append(contentsOf: try codec.append(Data([byte]))) }
        #expect(received == [payload])
        try codec.finish()
    }

    @Test func testOversizedHeaderFailsBeforeReadingBody() {
        var codec = RecordingFrameCodec(maximumBytes: 8)
        #expect(throws: RecordingContractError.self) { try codec.append(Data([0, 0, 0, 9])) }
    }

    @Test func testTruncatedPayloadFailsAtEOF() throws {
        var codec = RecordingFrameCodec()
        _ = try codec.append(Data([0, 0, 0, 2, 0]))
        #expect(throws: RecordingContractError.self) { try codec.finish() }
    }

    @Test func testEmptyOutgoingPayloadFails() {
        #expect(throws: RecordingContractError.self) { try RecordingFrameCodec.encode(Data()) }
    }

    @Test func testTwoFramesShareOneRead() throws {
        let payload = Data("{}".utf8)
        let encoded = try RecordingFrameCodec.encode(payload)
        var codec = RecordingFrameCodec()
        #expect(try codec.append(encoded + encoded) == [payload, payload])
    }
}
