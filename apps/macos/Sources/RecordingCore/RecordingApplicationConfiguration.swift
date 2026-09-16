import CryptoKit
import Foundation
import RecordingContracts

/// Canonical private application launch configuration. Python binds catalog/build digests;
/// this loader validates strict shape, private manifests and creates fresh process identities.
public struct RecordingApplicationConfiguration: Sendable {
    public let assetRoot: URL
    public let schemaDirectory: URL
    public let catalog: RecordingJSON
    public let catalogSHA256: String
    public let subjectID: String
    public let captureDevice: RecordingJSON
    public let captureHost: String
    public let pilotApprovalPath: RecordingJSON
    public let pilotApprovalSHA256: RecordingJSON
    public let recorder: RecordingProcessConfiguration
    public let service: RecordingProcessConfiguration

    /// A sealed bundle carries only this small locator; identities live outside its signature.
    public static func configurationURL(bundle: Bundle = .main, arguments: [String] = CommandLine.arguments) throws -> URL {
        if let index = arguments.firstIndex(of: "--configuration") {
            guard arguments.filter({ $0 == "--configuration" }).count == 1, index + 1 < arguments.count,
                  arguments[index + 1].hasPrefix("/") else { throw invalid("Explicit configuration requires one absolute path") }
            return URL(fileURLWithPath: arguments[index + 1])
        }
        guard let locator = bundle.url(forResource: "RecordingConfigurationPath", withExtension: "json") else { throw invalid("The application configuration locator is missing") }
        let data = try Data(contentsOf: locator)
        guard data.count <= 8192 else { throw invalid("Application configuration locator exceeds its bound") }
        let object = try object(RecordingJSON.parse(data), keys: ["schema_version", "configuration_path"])
        guard object["schema_version"] == .string("cole-voice-lab.recording-configuration-path/v1") else { throw invalid("Application locator version is unsupported") }
        return URL(fileURLWithPath: try absolutePath(object, "configuration_path"))
    }

    public static func load(privateFile: URL) throws -> Self {
        try Self(value: RecordingJSON.parse(RecordingPrivateFile.read(privateFile.path)))
    }

    private init(value: RecordingJSON) throws {
        let config = try Self.object(value, keys: ["schema_version", "asset_root", "schema_directory", "client_build", "client_build_path", "catalog", "catalog_sha256", "subject_id", "capture_device", "capture_host", "pilot_approval_path", "pilot_approval_sha256", "recorder", "service"])
        guard config["schema_version"] == .string("cole-voice-lab.recording-application/v1") else { throw Self.invalid("Application configuration version is unsupported") }
        assetRoot = URL(fileURLWithPath: try Self.absolutePath(config, "asset_root"), isDirectory: true)
        schemaDirectory = URL(fileURLWithPath: try Self.absolutePath(config, "schema_directory"), isDirectory: true)
        catalog = config["catalog"]!; catalogSHA256 = try Self.string(config, "catalog_sha256")
        subjectID = try Self.string(config, "subject_id"); captureDevice = config["capture_device"]!
        captureHost = try Self.string(config, "capture_host")
        pilotApprovalPath = config["pilot_approval_path"]!; pilotApprovalSHA256 = config["pilot_approval_sha256"]!
        let client = config["client_build"]!
        let clientPath = try Self.absolutePath(config, "client_build_path")
        guard try RecordingJSON.parse(RecordingPrivateFile.read(clientPath)) == client else { throw Self.invalid("Application build manifest differs from configuration") }
        let executable = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        let digest = SHA256.hash(data: try Data(contentsOf: executable, options: .mappedIfSafe)).map { String(format: "%02x", $0) }.joined()
        guard client.object?["executable_sha256"] == .string(digest) else { throw Self.invalid("Application executable differs from its immutable build identity") }
        recorder = try Self.peer(config["recorder"]!, role: .recorder, assetRoot: assetRoot,
                                 schemas: schemaDirectory, client: client, clientPath: clientPath, catalogSHA256: catalogSHA256)
        service = try Self.peer(config["service"]!, role: .service, assetRoot: assetRoot,
                                schemas: schemaDirectory, client: client, clientPath: clientPath, catalogSHA256: catalogSHA256)
        // Reuse the Python-owned command schema, including nested catalog/device and digest shapes.
        let schema = try Data(contentsOf: schemaDirectory.appendingPathComponent("recording-service-request.schema.json"))
        let command = openContextCommand(operationID: "application-config-check")
        let envelope = RecordingJSON.object(["data": command, "error": .null,
            "meta": .object(["protocol_version": .string("cole-voice-lab.recording/v1"),
                "request_id": .string("application-config-check"), "process_generation": .string(service.processGeneration),
                "state_revision": .integer(0), "event_sequence": .integer(0), "take_id": .null])])
        _ = try RecordingWireEnvelope.decode(envelope.transportData(), schema: schema)
    }

    /// Open a new process context; corpus authorization remains Python-owned and prepared there.
    public func openContextCommand(operationID: String, expectedSelectionRevision: Int64? = nil,
                                   newSetup: Bool = false, setupDescription: String = "") -> RecordingJSON {
        .object(["command": .string("open-context"), "operation_id": .string(operationID),
            "subject_id": .string(subjectID), "catalog": catalog, "catalog_sha256": .string(catalogSHA256),
            "capture_device": captureDevice, "capture_host": .string(captureHost),
            "expected_selection_revision": expectedSelectionRevision.map(RecordingJSON.integer) ?? .null,
            "new_setup": .boolean(newSetup), "setup_description": .string(setupDescription),
            "pilot_approval_path": pilotApprovalPath, "pilot_approval_sha256": pilotApprovalSHA256])
    }

    private static func peer(_ value: RecordingJSON, role: RecordingProcessConfiguration.Peer,
                             assetRoot: URL, schemas: URL, client: RecordingJSON,
                             clientPath: String, catalogSHA256: String) throws -> RecordingProcessConfiguration {
        let fields = try object(value, keys: ["executable", "arguments", "environment", "build", "build_identity_path"])
        guard let argv = fields["arguments"]?.array, argv.count <= 32, argv.allSatisfy({ ($0.string?.count ?? 4097) <= 4096 }),
              !argv.contains(where: { ["--asset-root", "--process-generation", "--build-identity", "--expected-client-build", "--schema-directory", "--catalog-sha256"].contains($0.string ?? "") }) else { throw invalid("Process base arguments are invalid or override canonical launch identity") }
        let build = fields["build"]!
        let buildPath = try absolutePath(fields, "build_identity_path")
        guard try RecordingJSON.parse(RecordingPrivateFile.read(buildPath)) == build else { throw invalid("Peer build manifest differs from configuration") }
        let generation = UUID().uuidString.lowercased()
        var arguments = argv.compactMap(\.string) + ["--asset-root", assetRoot.path, "--process-generation", generation,
            "--build-identity", buildPath, "--expected-client-build", clientPath]
        if role == .recorder { arguments += ["--schema-directory", schemas.path] }
        else { arguments += ["--catalog-sha256", catalogSHA256] }
        var environment: [String: String]?
        if fields["environment"] != .null {
            guard let entries = fields["environment"]?.object, entries.count <= 64,
                  entries.allSatisfy({ !$0.key.isEmpty && $0.key.count <= 128 && ($0.value.string?.count ?? 8193) <= 8192 }) else { throw invalid("Process environment must be a bounded string map or null") }
            environment = entries.mapValues { $0.string! }
        }
        return RecordingProcessConfiguration(executable: URL(fileURLWithPath: try absolutePath(fields, "executable")),
            arguments: arguments, environment: environment, schemaDirectory: schemas,
            processGeneration: generation, clientBuild: client, expectedPeerBuild: build, peer: role)
    }

    private static func object(_ value: RecordingJSON, keys: Set<String>) throws -> [String: RecordingJSON] {
        guard let object = value.object, Set(object.keys) == keys else { throw invalid("Application configuration has missing or unknown fields") }
        return object
    }
    private static func string(_ value: [String: RecordingJSON], _ key: String) throws -> String {
        guard let text = value[key]?.string, !text.isEmpty, text.count <= 4096 else { throw invalid("Application configuration requires bounded nonempty string: \(key)") }
        return text
    }
    private static func absolutePath(_ value: [String: RecordingJSON], _ key: String) throws -> String {
        let path = try string(value, key)
        guard path.hasPrefix("/"), !path.split(separator: "/").contains(".."), !path.split(separator: "/").contains("."),
              URL(fileURLWithPath: path).path == path else { throw invalid("Application path must be normalized and absolute: \(key)") }
        return path
    }
    private static func invalid(_ message: String) -> RecordingContractError {
        RecordingContractError(code: "RECORDING.INVALID_MESSAGE", message: message)
    }
}
