import Darwin
import Foundation
@testable import RecordingCore
import RecordingContracts
import Testing

@Suite struct RecordingBridgeTests {
    private func meta(_ sequence: Int64, revision: Int64 = 0, generation: String = "generation") throws -> RecordingMessageMeta {
        try JSONDecoder().decode(RecordingMessageMeta.self, from: JSONEncoder().encode([
            "protocol_version": RecordingJSON.string("cole-voice-lab.recording/v1"),
            "request_id": .string("request"), "process_generation": .string(generation),
            "state_revision": .integer(revision), "event_sequence": .integer(sequence), "take_id": .null,
        ]))
    }

    @Test func gapRequiresSnapshotAndStaleEventsCannotRewind() throws {
        var guarder = RecordingEventGuard(generation: "generation")
        #expect(try guarder.receive(meta(1), snapshot: true) == .apply)
        #expect(try guarder.receive(meta(3, revision: 2), snapshot: false) == .snapshotRequired)
        #expect(try guarder.receive(meta(4, revision: 2), snapshot: true) == .apply)
        #expect(try guarder.receive(meta(3, revision: 2), snapshot: true) == .stale)
        #expect(try guarder.receive(meta(4, revision: 2), snapshot: false) == .duplicate)
        #expect(try guarder.receive(meta(5, revision: 1), snapshot: false) == .stale)
        #expect(try guarder.receive(meta(5, revision: 2), snapshot: false) == .apply)
        #expect(throws: RecordingContractError.self) { try guarder.receive(meta(6, generation: "old"), snapshot: false) }
    }

    @Test func maximumSignedSequenceDoesNotOverflow() throws {
        var guarder = RecordingEventGuard(generation: "generation")
        #expect(try guarder.receive(meta(.max, revision: .max), snapshot: true) == .apply)
        #expect(try guarder.receive(meta(.max, revision: .max), snapshot: false) == .duplicate)
    }

    @Test func explicitRequestReplaysExactOriginalBytes() throws {
        var cache = RecordingRequestReplayCache(maximumEntries: 1)
        let command: RecordingJSON = .object(["command": .string("snapshot")])
        let first = try cache.payload(requestID: "request", command: command, takeID: nil, envelope: .integer(1))
        #expect(try cache.payload(requestID: "request", command: command, takeID: nil, envelope: .integer(99)) == first)
        #expect(throws: RecordingContractError.self) { try cache.payload(requestID: "request", command: .null, takeID: nil, envelope: .null) }
        #expect(throws: RecordingContractError.self) { try cache.payload(requestID: "request", command: command, takeID: "other-take", envelope: .null) }
        #expect(throws: RecordingContractError.self) { try cache.payload(requestID: "second", command: command, takeID: nil, envelope: .null) }
        #expect(try cache.payload(requestID: nil, command: command, takeID: nil, envelope: .integer(2)) == Data("2".utf8))
    }

    @Test func pipeReaderReturnsAvailableBytesBeforeEOF() throws {
        let pipe = Pipe()
        defer { try? pipe.fileHandleForReading.close(); try? pipe.fileHandleForWriting.close() }
        try pipe.fileHandleForWriting.write(contentsOf: Data([1, 2, 3]))
        var reader = RecordingPipeReader(descriptor: pipe.fileHandleForReading.fileDescriptor)
        #expect(try reader.next() == Data([1, 2, 3]))
        try pipe.fileHandleForWriting.close()
        #expect(try reader.next() == nil)
    }

    @Test func privateFileRejectsSymlinksPublicModesAndOversize() throws {
        let lexical = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: lexical, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        guard let resolved = realpath(lexical.path, nil) else { throw NSError(domain: "fixture", code: 1) }
        let root = URL(fileURLWithPath: String(cString: resolved)); free(resolved)
        defer { try? FileManager.default.removeItem(at: root) }
        let fixture = root.appendingPathComponent("fixture.json")
        try Data("{}".utf8).write(to: fixture)
        guard chmod(fixture.path, 0o600) == 0 else { throw NSError(domain: "fixture", code: 2) }
        #expect(try RecordingPrivateFile.read(fixture.path) == Data("{}".utf8))
        #expect(throws: RecordingContractError.self) { try RecordingPrivateFile.read(fixture.path, maximumBytes: 1) }
        let link = root.appendingPathComponent("link")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: fixture)
        #expect(throws: RecordingContractError.self) { try RecordingPrivateFile.read(link.path) }
        guard chmod(fixture.path, 0o644) == 0 else { throw NSError(domain: "fixture", code: 3) }
        #expect(throws: RecordingContractError.self) { try RecordingPrivateFile.read(fixture.path) }
    }
}
