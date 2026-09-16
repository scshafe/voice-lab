import RecordingCore
import Testing

@Suite struct RecordingRuntimeTests {
    @Test func testC11BoundaryABIIsLinked() {
        #expect(RecordingRuntime.captureBufferABI == 1)
    }
}
