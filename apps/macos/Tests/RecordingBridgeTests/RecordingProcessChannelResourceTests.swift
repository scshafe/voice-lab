import CryptoKit
import Darwin
import Foundation
import RecordingContracts
import RecordingCore
import Testing

private actor TelemetryWatermark {
    private var sequence: Int64 = 0
    func receive(_ value: Int64) { sequence = max(sequence, value) }
    func reached(_ value: Int64) -> Bool { sequence >= value }
}

/// Run separately from other native suites: process footprint is the measured boundary.
/// The peer stays alive at both samples, so EOF cannot hide reader-owned allocations.
@Suite(.serialized) struct RecordingProcessChannelResourceTests {
    private let repository = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()

    private func footprint(_ pid: pid_t = getpid()) throws -> UInt64 {
        var usage = rusage_info_v4()
        let status = withUnsafeMutablePointer(to: &usage) { pointer in
            pointer.withMemoryRebound(to: rusage_info_t?.self, capacity: 1) {
                proc_pid_rusage(pid, RUSAGE_INFO_V4, $0)
            }
        }
        try #require(status == 0)
        return usage.ri_phys_footprint
    }

    private func channel(mode: String) throws -> RecordingProcessChannel {
        let python = repository.appendingPathComponent(".venv/bin/python").resolvingSymlinksInPath()
        let digest = SHA256.hash(data: try Data(contentsOf: python)).map { String(format: "%02x", $0) }.joined()
        let build: RecordingJSON = .object([
            "build_id": .string("synthetic-resource-test"),
            "source_sha256": .string(String(repeating: "a", count: 64)),
            "executable_sha256": .string(digest),
            "environment_sha256": .string(String(repeating: "b", count: 64)),
        ])
        return RecordingProcessChannel(configuration: RecordingProcessConfiguration(
            executable: python,
            arguments: [repository.appendingPathComponent("tests/native_recording_channel_resource_fixture.py").path, mode],
            schemaDirectory: repository.appendingPathComponent("schemas"),
            processGeneration: "synthetic-resource-generation", clientBuild: build,
            expectedPeerBuild: build, peer: .recorder
        ))
    }

    private func helperChannel(root: URL) throws -> RecordingProcessChannel {
        let helperPath = try #require(ProcessInfo.processInfo.environment["COLE_VOICE_LAB_TEST_HELPER_EXECUTABLE"],
            "Resource suite requires the verifier's explicit built helper path")
        let helper = URL(fileURLWithPath: helperPath)
        try #require(FileManager.default.fileExists(atPath: helper.path))
        let digest = SHA256.hash(data: try Data(contentsOf: helper)).map { String(format: "%02x", $0) }.joined()
        let build: RecordingJSON = .object([
            "build_id": .string("synthetic-helper-resource-test"),
            "source_sha256": .string(String(repeating: "a", count: 64)),
            "executable_sha256": .string(digest),
            "environment_sha256": .string(String(repeating: "b", count: 64)),
        ])
        let identity = root.appendingPathComponent("build.json")
        try build.transportData().write(to: identity, options: .withoutOverwriting)
        let pcm = root.appendingPathComponent("synthetic.pcm")
        try Data([0, 0, 0]).write(to: pcm, options: .withoutOverwriting)
        for file in [identity, pcm] { try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path) }
        let generation = "synthetic-helper-resource-generation"
        let schemas = repository.appendingPathComponent("schemas")
        return RecordingProcessChannel(configuration: RecordingProcessConfiguration(
            executable: helper,
            arguments: ["--asset-root", root.path, "--process-generation", generation,
                "--build-identity", identity.path, "--expected-client-build", identity.path,
                "--schema-directory", schemas.path, "--synthetic-pcm", pcm.path],
            schemaDirectory: schemas, processGeneration: generation,
            clientBuild: build, expectedPeerBuild: build, peer: .recorder
        ))
    }

    private func request(_ channel: RecordingProcessChannel, identifier: String?) async throws {
        _ = try await channel.request(.object(["command": .string("snapshot")]), takeID: nil,
            requestID: identifier, timeout: 60)
    }

    private func awaitTelemetry(_ watermark: TelemetryWatermark, sequence: Int64) async throws {
        let limit = ContinuousClock.now.advanced(by: .seconds(60))
        while !(await watermark.reached(sequence)) {
            try #require(ContinuousClock.now < limit, "Synthetic telemetry reader did not reach its completion marker")
            try await Task.sleep(for: .milliseconds(5))
        }
    }

    @Test func reliableReaderReleasesTransientAllocationsBeforeItsPeerCloses() async throws {
        let channel = try channel(mode: "reliable")
        let consumer = Task { for try await _ in channel.events {} }
        defer { channel.close(); consumer.cancel() }
        try await channel.start()
        for _ in 0..<16 { try await request(channel, identifier: nil) }
        let before = try footprint()
        for _ in 0..<1024 { try await request(channel, identifier: nil) }
        let after = try footprint()
        let growth = after > before ? after - before : 0
        print("CHANNEL_RESOURCE reliable before=\(before) after=\(after) growth=\(growth) messages=1024 peer_alive=\(kill(channel.processIdentifier, 0) == 0)")
        #expect(kill(channel.processIdentifier, 0) == 0)
        // Section 12 already limits recorder-stack growth to 64 MiB. No extra budget
        // is granted to this single reader after its one-time startup allocations.
        #expect(growth <= 64 * 1024 * 1024)
    }

    @Test func helperReaderReleasesTransientAllocationsBeforeItsControlPipeCloses() async throws {
        let temporary = try #require(realpath(FileManager.default.temporaryDirectory.path, nil))
        defer { free(temporary) }
        let root = URL(fileURLWithPath: String(cString: temporary), isDirectory: true)
            .appendingPathComponent("cole-channel-resource-\(UUID().uuidString.lowercased())")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: root) }
        let channel = try helperChannel(root: root)
        let consumer = Task { for try await _ in channel.events {} }
        defer { channel.close(); consumer.cancel() }
        try await channel.start()
        for _ in 0..<16 { try await request(channel, identifier: nil) }
        let before = try footprint(channel.processIdentifier)
        for _ in 0..<4096 { try await request(channel, identifier: nil) }
        let after = try footprint(channel.processIdentifier)
        let growth = after > before ? after - before : 0
        print("CHANNEL_RESOURCE helper before=\(before) after=\(after) growth=\(growth) messages=4096 peer_alive=\(kill(channel.processIdentifier, 0) == 0)")
        #expect(kill(channel.processIdentifier, 0) == 0)
        #expect(growth <= 64 * 1024 * 1024)
    }

    @Test func telemetryReaderReleasesTransientAllocationsBeforeItsPeerCloses() async throws {
        let channel = try channel(mode: "telemetry")
        let consumer = Task { for try await _ in channel.events {} }
        let watermark = TelemetryWatermark()
        let telemetry = Task {
            for try await event in channel.telemetry { await watermark.receive(event.meta.event_sequence) }
        }
        defer { channel.close(); consumer.cancel(); telemetry.cancel() }
        try await channel.start()
        try await request(channel, identifier: "warmup")
        try await awaitTelemetry(watermark, sequence: 128)
        let before = try footprint()
        try await request(channel, identifier: "measurement")
        try await awaitTelemetry(watermark, sequence: 128 + 4096)
        let after = try footprint()
        let growth = after > before ? after - before : 0
        print("CHANNEL_RESOURCE telemetry before=\(before) after=\(after) growth=\(growth) messages=4096 peer_alive=\(kill(channel.processIdentifier, 0) == 0)")
        #expect(kill(channel.processIdentifier, 0) == 0)
        #expect(growth <= 64 * 1024 * 1024)
    }
}
