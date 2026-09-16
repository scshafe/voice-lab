import CryptoKit
import Darwin
import Foundation
import RecordingContracts
@testable import RecordingCore
import Testing

private final class SchemaPreparations: @unchecked Sendable {
    private let lock = NSLock()
    private var titles: [String] = []
    func record(_ title: String) { lock.lock(); defer { lock.unlock() }; titles.append(title) }
    var recorded: [String] { lock.lock(); defer { lock.unlock() }; return titles }
}

private actor PreparedTelemetryWatermark {
    private var sequence: Int64 = 0
    func receive(_ value: Int64) { sequence = max(sequence, value) }
    func reached(_ value: Int64) -> Bool { sequence >= value }
}

@Suite(.serialized) struct RecordingPreparedChannelTests {
    private func channel(mode: String = "reliable",
                         prepare: @escaping @Sendable (Data) throws -> RecordingSchemaValidator) throws -> RecordingProcessChannel {
        let repository = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let python = repository.appendingPathComponent(".venv/bin/python").resolvingSymlinksInPath()
        let digest = SHA256.hash(data: try Data(contentsOf: python)).map { String(format: "%02x", $0) }.joined()
        let build: RecordingJSON = .object(["build_id": .string("synthetic-prepared-schema-test"),
            "source_sha256": .string(String(repeating: "a", count: 64)),
            "executable_sha256": .string(digest), "environment_sha256": .string(String(repeating: "b", count: 64))])
        let configuration = RecordingProcessConfiguration(executable: python,
            arguments: [repository.appendingPathComponent("tests/native_recording_channel_resource_fixture.py").path, mode],
            schemaDirectory: repository.appendingPathComponent("schemas"),
            processGeneration: "synthetic-prepared-generation", clientBuild: build,
            expectedPeerBuild: build, peer: .recorder)
        return RecordingProcessChannel(configuration: configuration, prepareSchema: prepare)
    }

    private func closeAndCheck(_ channel: RecordingProcessChannel) async throws {
        channel.close()
        let pid = channel.processIdentifier
        guard pid > 0 else { return }
        let deadline = ContinuousClock.now.advanced(by: .seconds(5))
        while kill(pid, 0) == 0 && ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(10)) }
        #expect(kill(pid, 0) != 0 && errno == ESRCH, "Owned synthetic peer must leave after EOF")
    }

    @Test(arguments: ["reliable", "telemetry"])
    func preparesExactlyThreeSchemasForRepeatedRequestsAndTelemetry(mode: String) async throws {
        let calls = SchemaPreparations()
        let channel = try channel(mode: mode, prepare: {
            calls.record(try RecordingJSON.parse($0).object?["title"]?.string ?? "missing")
            return try RecordingSchemaValidator(preparing: $0)
        })
        let events = Task { for try await _ in channel.events {} }
        let watermark = PreparedTelemetryWatermark()
        let telemetry = Task { for try await event in channel.telemetry { await watermark.receive(event.meta.event_sequence) } }
        defer { channel.close(); events.cancel(); telemetry.cancel() }
        try await channel.start()
        if mode == "telemetry" {
            _ = try await channel.request(.object(["command": .string("snapshot")]), takeID: nil, requestID: "warmup")
            let deadline = ContinuousClock.now.advanced(by: .seconds(5))
            while !(await watermark.reached(128)) && ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(5)) }
            #expect(await watermark.reached(128))
        } else {
            for _ in 0..<8 { _ = try await channel.request(.object(["command": .string("snapshot")]), takeID: nil) }
        }
        #expect(calls.recorded == ["RecorderRequest", "RecordingResponse", "RecordingTelemetry"])
        try await closeAndCheck(channel)
    }

    @Test(arguments: ["RecorderRequest", "RecordingResponse", "RecordingTelemetry"])
    func eachTransportUsesItsRetainedPreparedValidator(title: String) async throws {
        let channel = try channel(mode: title == "RecordingTelemetry" ? "telemetry" : "reliable", prepare: { schema in
            guard try RecordingJSON.parse(schema).object?["title"]?.string == title else {
                return try RecordingSchemaValidator(preparing: schema)
            }
            // Handshake has neither action nor waveform event. Reject only the
            // selected transport's subsequent operation through its retained validator.
            let restriction: String
            if title == "RecorderRequest" {
                restriction = #"{"type":"object","properties":{"data":{"type":"object","properties":{"command":{"const":"handshake"}}}}}"#
            } else if title == "RecordingResponse" {
                restriction = #"{"type":"object","properties":{"data":{"type":"object","properties":{"action":{"const":"forbidden-recovered"}}}}}"#
            } else {
                restriction = #"{"type":"object","properties":{"data":{"type":"object","properties":{"event":{"const":"forbidden-waveform"}}}}}"#
            }
            return try RecordingSchemaValidator(preparing: Data(restriction.utf8))
        })
        let events = Task { for try await _ in channel.events {} }
        let telemetry = Task { () -> String? in
            do {
                for try await event in channel.telemetry {
                    if event.meta.event_sequence >= 128 { return "UNEXPECTED_ACCEPTED_TELEMETRY" }
                }
                return nil
            }
            catch let failure as RecordingContractError { return failure.code }
            catch { return String(describing: error) }
        }
        defer { channel.close(); events.cancel(); telemetry.cancel() }
        try await channel.start()
        var requestFailure: String?
        do { _ = try await channel.request(.object(["command": .string("snapshot")]), takeID: nil, requestID: "warmup") }
        catch let failure as RecordingContractError { requestFailure = failure.code }
        if title == "RecordingTelemetry" {
            #expect(await telemetry.value == "RECORDING.INVALID_MESSAGE")
        } else {
            #expect(requestFailure == "RECORDING.INVALID_MESSAGE")
        }
        try await closeAndCheck(channel)
    }

    @Test func schemaPreparationFailurePreventsPeerLaunch() async throws {
        let channel = try channel(prepare: { _ in
            throw RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: "Synthetic preparation failure")
        })
        defer { channel.close() }
        var message: String?
        do { try await channel.start() }
        catch let failure as RecordingContractError { message = failure.message }
        #expect(message == "Synthetic preparation failure")
        #expect(channel.processIdentifier == 0)
    }

    @Test func secondStartCannotReplacePublishedValidators() async throws {
        let calls = SchemaPreparations()
        let channel = try channel(prepare: {
            calls.record(try RecordingJSON.parse($0).object?["title"]?.string ?? "missing")
            return try RecordingSchemaValidator(preparing: $0)
        })
        let events = Task { for try await _ in channel.events {} }
        defer { channel.close(); events.cancel() }
        try await channel.start()
        var failure: String?
        do { try await channel.start() }
        catch let error as RecordingContractError { failure = error.message }
        #expect(failure == "Local process channel was already started")
        #expect(calls.recorded == ["RecorderRequest", "RecordingResponse", "RecordingTelemetry"])
        try await closeAndCheck(channel)
    }
}
