import Darwin
import Foundation
import RecordingContracts
import RecordingCore

/// Actual native/Python subprocess gate using only explicitly supplied synthetic PCM.
@main
struct RecordingBridgeHarness {
    static func main() async throws {
        umask(0o077)
        guard CommandLine.arguments.count == 2 else { throw failure("Bridge harness requires one private synthetic fixture manifest") }
        let manifestURL = URL(fileURLWithPath: CommandLine.arguments[1])
        let manifest = try RecordingJSON.parse(Data(contentsOf: manifestURL))
        guard let fields = manifest.object else { throw failure("Bridge manifest must be an object") }
        func text(_ name: String) throws -> String {
            guard let value = fields[name]?.string else { throw failure("Bridge manifest field missing: \(name)") }
            return value
        }
        func arguments(_ name: String) throws -> [String] {
            guard let array = fields[name]?.array else { throw failure("Bridge arguments missing") }
            let strings = array.compactMap(\.string)
            guard strings.count == array.count else { throw failure("Bridge arguments must be strings") }
            return strings
        }
        let helperArguments = try arguments("helper_arguments")
        guard helperArguments.contains("--synthetic-pcm") else { throw failure("Bridge refuses a helper without explicit synthetic input") }
        let schemaDirectory = URL(fileURLWithPath: try text("schema_directory"), isDirectory: true)
        let generation = try text("recorder_generation")
        let clientBuild = fields["client_build"]!
        let recorder = RecorderClient(configuration: RecordingProcessConfiguration(
            executable: URL(fileURLWithPath: try text("helper_executable")), arguments: helperArguments,
            schemaDirectory: schemaDirectory, processGeneration: generation,
            clientBuild: clientBuild, expectedPeerBuild: fields["helper_build"]!, peer: .recorder
        ))
        let environment = fields["service_environment"]?.object?.mapValues { $0.string ?? "" }
        func makeService(reconnecting: Bool = false) throws -> RecordingServiceClient {
            var serviceArguments = try arguments("service_arguments")
            let serviceGeneration = reconnecting ? "synthetic-service-reconnected" : try text("service_generation")
            if reconnecting, let index = serviceArguments.firstIndex(of: "--process-generation") { serviceArguments[index + 1] = serviceGeneration }
            return RecordingServiceClient(configuration: RecordingProcessConfiguration(
            executable: URL(fileURLWithPath: try text("python_executable")), arguments: serviceArguments,
            environment: environment, schemaDirectory: schemaDirectory,
            processGeneration: serviceGeneration, clientBuild: clientBuild,
            expectedPeerBuild: fields["service_build"]!, peer: .service
        ))
        }
        var service = try makeService()
        try await recorder.connect()
        try await service.connect()
        let history = try await service.snapshot()
        guard history.error == .null, history.data.object?["completeness"] == .string("unknown") else {
            throw failure("History remains explicitly unknown until its separate index is attached")
        }
        let reserved = try await service.reserve(groupID: text("group_id"), recorderGeneration: generation, selection: fields["selection"]!, operationID: "synthetic-reserve")
        guard reserved.error == .null, reserved.data.object?["event"] == .string("reserved"),
              let reservation = reserved.data.object?["take"]?.object?["reservation"],
              let digest = reserved.data.object?["reservation_sha256"]?.string else {
            throw failure("Service did not return its durable reservation and canonical identity")
        }
        let takeID = reservation.object!["take_id"]!.string!
        let started = try await recorder.start(reservation: reservation, reservationSHA256: digest)
        guard started.error == .null else { throw failure("Native subprocess refused synthetic Start: \(started.error)") }
        let duplicateStart = try await recorder.start(reservation: reservation, reservationSHA256: digest)
        guard duplicateStart.error == .null else { throw failure("Duplicate Start did not resolve the existing take") }
        let expectedPCM = try Data(contentsOf: URL(fileURLWithPath: text("synthetic_pcm")))
        let expectedFrames = Int64(expectedPCM.count / 3)
        let live = try await waitFor(recorder.channel.telemetry) { envelope in
            (envelope.data.object?["written_frames"]?.integer ?? 0) >= expectedFrames
        }
        guard live.data.object?["take_id"] == .string(takeID) else { throw failure("Telemetry changed take identity") }
        // Kill only the synthetic service launched above; recorder Stop must remain independent.
        guard kill(service.channel.processIdentifier, SIGKILL) == 0 else { throw failure("Could not inject owned synthetic service death") }
        let deadService = service
        let stop = try await recorder.stop(takeID: takeID, operationID: "synthetic-stop")
        guard stop.error == .null else { throw failure("Native subprocess refused Stop") }
        let repeatedStop = try await recorder.stop(takeID: takeID, operationID: "synthetic-stop")
        guard repeatedStop.error == .null else { throw failure("Duplicate Stop did not resolve its existing state") }
        let draft: RecordingWireEnvelope
        if repeatedStop.data.object?["transport"] == .string("draft") { draft = repeatedStop }
        else {
            draft = try await waitFor(recorder.channel.events) { $0.data.object?["transport"] == .string("draft") }
        }
        guard let seal = draft.data.object?["seal"], seal != .null,
              seal.object?["audio"]?.object?["frames"] == .integer(expectedFrames) else {
            throw failure("Sealed source frame count differs from exact synthetic input")
        }
        let assetRoot = URL(fileURLWithPath: try text("asset_root"), isDirectory: true)
        let original = assetRoot.appendingPathComponent(seal.object!["original_path"]!.string!)
        let bytes = try Data(contentsOf: original)
        guard bytes.count == expectedPCM.count + 44, Data(bytes.dropFirst(44)) == expectedPCM else {
            throw failure("Subprocess/C-ring/WAV writer changed exact synthetic PCM")
        }
        var serviceDeathDetected = false
        do { _ = try await deadService.snapshot() } catch { serviceDeathDetected = true }
        guard serviceDeathDetected else { throw failure("Native service client did not observe subprocess death") }
        service = try makeService(reconnecting: true)
        try await service.connect()
        let reconnected = try await service.snapshot()
        guard reconnected.error == .null, reconnected.data.object?["completeness"] == .string("unknown") else { throw failure("Reconnected service did not provide an authoritative snapshot") }
        let registration = try await service.registerSeal(seal, operationID: "synthetic-register")
        guard registration.error == .null, registration.data.object?["take"]?.object?["decision"] == .string("draft"),
              registration.data.object?["take"]?.object?["analysis"] == .string("pending") else {
            throw failure("Service did not durably register an independent unkept draft")
        }
        let replay = try await service.registerSeal(seal, operationID: "synthetic-register")
        guard replay.error == .null, replay.data == registration.data else { throw failure("Durable seal replay changed its result") }
        _ = try await service.shutdown()
        _ = try await recorder.shutdown()
        // A fresh idle helper proves client EOF handling without recapturing an existing take.
        let doomed = RecorderClient(configuration: RecordingProcessConfiguration(
            executable: URL(fileURLWithPath: try text("helper_executable")), arguments: helperArguments,
            schemaDirectory: schemaDirectory, processGeneration: generation,
            clientBuild: clientBuild, expectedPeerBuild: fields["helper_build"]!, peer: .recorder
        ))
        try await doomed.connect()
        guard kill(doomed.channel.processIdentifier, SIGKILL) == 0 else { throw failure("Could not inject owned synthetic helper death") }
        var helperDeathDetected = false
        do { _ = try await doomed.snapshot() } catch { helperDeathDetected = true }
        guard helperDeathDetected else { throw failure("Native recorder client did not observe helper death") }
        let output = manifestURL.deletingLastPathComponent()
        try seal.transportData().write(to: output.appendingPathComponent("native-seal.json"), options: .atomic)
        try reserved.data.transportData().write(to: output.appendingPathComponent("service-reservation.json"), options: .atomic)
        try registration.data.transportData().write(to: output.appendingPathComponent("service-registration.json"), options: .atomic)
        let report: RecordingJSON = .object([
            "synthetic_only": .boolean(true), "real_helper_process": .boolean(true),
            "real_service_process": .boolean(true), "microphone_opened": .boolean(false),
            "exact_pcm_frames": .integer(expectedFrames), "duplicate_start_stop": .boolean(true),
            "service_death_independent_stop": .boolean(true), "service_reconnect_snapshot": .boolean(true),
            "helper_death_detected": .boolean(true),
            "service_registration": .string("durable-unkept-draft"),
            "service_reservation_digest_consumed": .boolean(true),
            "native_helper_pid": .integer(Int64(recorder.channel.processIdentifier)),
            "python_service_pid": .integer(Int64(service.channel.processIdentifier)),
        ])
        try report.transportData().write(to: output.appendingPathComponent("bridge-result.json"), options: .atomic)
        print(String(decoding: try report.transportData(), as: UTF8.self))
    }

    private static func waitFor(
        _ stream: AsyncThrowingStream<RecordingWireEnvelope, Error>,
        predicate: @escaping @Sendable (RecordingWireEnvelope) -> Bool
    ) async throws -> RecordingWireEnvelope {
        try await withThrowingTaskGroup(of: RecordingWireEnvelope.self) { group in
            group.addTask {
                for try await event in stream { if predicate(event) { return event } }
                throw failure("Process stream ended before the expected state")
            }
            group.addTask {
                try await Task.sleep(for: .seconds(5))
                throw failure("Process event deadline expired")
            }
            guard let result = try await group.next() else { throw failure("Missing process result") }
            group.cancelAll()
            return result
        }
    }

    private static func failure(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.BRIDGE_VERIFICATION", message: message)
    }
}
