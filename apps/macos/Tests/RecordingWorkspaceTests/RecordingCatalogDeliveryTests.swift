import Foundation
import RecordingContracts
import RecordingCore
@testable import RecordingUI
import Testing

/// The workspace's catalog comes from the service, never from its configuration: after the
/// handshake it requests the pinned catalog, builds the Library from it, takes the snapshot and
/// opens its context by the delivered reference. Replies are scripted; no process is created.
@Suite(.serialized)
struct RecordingCatalogDeliveryTests {
    /// Records every bootstrap request in order and answers each with its scripted reply.
    @MainActor private final class ScriptedService {
        var sent: [RecordingJSON] = []
        var refusedCatalogs = 0
        var refusedOpens = 0
        var inventoryRequests = 0
        var catalog: RecordingJSON = RecordingCatalogDeliveryTests.pilotCatalog()
        var reference: RecordingJSON = RecordingCatalogDeliveryTests.reference()
        /// The stored cursor's selection revision (nil: the library has no cursor yet). The
        /// snapshot reports it and, as in the real take store, open-context must expect exactly
        /// it; each opened context advances it.
        var durableRevision: Int64?
        var commands: [String] { sent.compactMap { $0.object?["command"]?.string } }

        var requests: RecordingContextRequests {
            RecordingContextRequests(
                catalog: { [self] operation in
                    sent.append(.object(["command": .string("catalog"), "operation_id": .string(operation)]))
                    if refusedCatalogs > 0 {
                        refusedCatalogs -= 1
                        return try RecordingCatalogDeliveryTests.errorReply("CORPUS.NO_ACTIVE_CATALOG", RecordingCatalogDeliveryTests.refusal)
                    }
                    return try RecordingCatalogDeliveryTests.reply(RecordingCatalogDeliveryTests.catalogEvent(operation, catalog: catalog, reference: reference))
                },
                snapshot: { [self] in
                    sent.append(.object(["command": .string("snapshot")]))
                    guard let durableRevision else {
                        return try RecordingCatalogDeliveryTests.reply(RecordingCatalogDeliveryTests.historyPage(), schema: nil)
                    }
                    return try RecordingCatalogDeliveryTests.reply(RecordingCatalogDeliveryTests.contextEvent(
                        operation: "earlier-context", promptID: "reading-two", digest: digest, revision: durableRevision), schema: nil)
                },
                openContext: { [self] command in
                    sent.append(command)
                    if refusedOpens > 0 {
                        refusedOpens -= 1
                        return try RecordingCatalogDeliveryTests.errorReply("RECORDING.WRITE_FAILED", "synthetic open-context failure")
                    }
                    guard command.object?["expected_selection_revision"]?.integer == durableRevision else {
                        return try RecordingCatalogDeliveryTests.errorReply("RECORDING.STALE_REVISION", RecordingCatalogDeliveryTests.staleRevision)
                    }
                    let revision = (durableRevision ?? -1) + 1
                    durableRevision = revision
                    return try RecordingCatalogDeliveryTests.reply(RecordingCatalogDeliveryTests.contextEvent(
                        operation: command.object?["operation_id"]?.string ?? "", promptID: "reading-one", digest: digest, revision: revision), schema: nil)
                })
        }

        private var digest: String { reference.object?["catalog_sha256"]?.string ?? "" }
    }

    static let digest = String(repeating: "c", count: 64)
    static let refusal = "subject synthetic-subject has no active catalog; run `voice-lab catalog init` or `voice-lab catalog activate`"
    /// recording_takes.open_context's refusal of any other expected revision.
    static let staleRevision = "workspace changed while context was being prepared"
    private static let repository = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
    private static let target = RecordingContextTarget(subjectID: "synthetic-subject",
        captureDevice: .object(["backend": .string("coreaudio"), "device_id": .string("synthetic-coreaudio-uid"),
                                "display_name": .string("Synthetic UMIK-1")]),
        captureHost: "synthetic-host")
    /// A capture-corpus build: its settings name a pilot approval.
    private static let approvedTarget = RecordingContextTarget(subjectID: "synthetic-subject",
        captureDevice: target.captureDevice, captureHost: "synthetic-host",
        pilotApprovalPath: .string("provenance/pilot-gate.json"), pilotApprovalSHA256: .string(String(repeating: "b", count: 64)))

    static func reference(purpose: String = "capture-pilot", suiteID: String = "synthetic-store",
                          digest: String = RecordingCatalogDeliveryTests.digest, version: Int64 = 1) -> RecordingJSON {
        .object(["catalog_id": .string("synthetic-store"), "catalog_version": .integer(version),
                 "catalog_sha256": .string(digest), "snapshot_sha256": .string(String(repeating: "d", count: 64)),
                 "suite_id": .string(suiteID), "purpose": .string(purpose),
                 "snapshot_path": .string("provenance/corpus/snapshots/\(digest).json")])
    }

    private static func prompt(_ id: String, category: String = "neutral", text: String? = nil,
                               instructions: String? = nil, labels: [String] = []) -> RecordingJSON {
        var object: [String: RecordingJSON] = ["prompt_id": .string(id), "category": .string(category),
                                               "style": .string("Natural pace"), "expected_seconds": .number(3)]
        if let text { object["text"] = .string(text) }
        if let instructions { object["instructions"] = .string(instructions) }
        if !labels.isEmpty { object["labels"] = .array(labels.map(RecordingJSON.string)) }
        return .object(object)
    }

    static func pilotCatalog() -> RecordingJSON {
        .object(["schema_version": .string("cole-voice-lab.prompt-suite/v2"), "suite_id": .string("synthetic-store"),
                 "purpose": .string("capture-pilot"), "prompts": .array([
                    prompt("room-tone-start", category: "room-tone", instructions: "Remain still and quiet."),
                    prompt("reading-one", text: "Read the first synthetic sentence."),
                    prompt("reading-two", text: "Read the second synthetic sentence."),
                 ])])
    }

    private static func physicalTestCatalog() -> RecordingJSON {
        .object(["schema_version": .string("cole-voice-lab.prompt-suite/v2"), "suite_id": .string("cole-physical-test-synthetic-store"),
                 "purpose": .string("physical-test"), "prompts": .array([
                    prompt("room-tone-start", category: "room-tone", instructions: "Remain still and quiet.", labels: ["test"]),
                    prompt("reading-one", text: "Read the first synthetic sentence.", labels: ["test"]),
                    prompt("unplug-device", category: "procedure", instructions: "Unplug the synthetic device.", labels: ["test", "device-loss"]),
                 ])])
    }

    static func catalogEvent(_ operation: String, catalog: RecordingJSON, reference: RecordingJSON) -> RecordingJSON {
        .object(["event": .string("catalog"), "operation_id": .string(operation), "catalog": catalog, "reference": reference])
    }

    static func historyPage(digest: String = RecordingCatalogDeliveryTests.digest) -> RecordingJSON {
        .object(["event": .string("history"), "catalog_sha256": .string(digest), "index_revision": .integer(3),
                 "completeness": .string("unknown"), "covered_prompt_count": .integer(0), "total_prompt_count": .integer(3),
                 "takes": .array([]), "prompts": .array([]), "next_cursor": .null, "next_prompt_cursor": .null])
    }

    static func contextEvent(operation: String, promptID: String, digest: String, revision: Int64) -> RecordingJSON {
        .object(["event": .string("context"),
                 "group": .object(["group_id": .string("group-store"), "gate": .object(["state": .string("not-required")])]),
                 "cursor": .object(["operation_id": .string(operation), "complete": .boolean(false),
                     "selection": .object(["catalog_sha256": .string(digest), "prompt_id": .string(promptID),
                                           "take_id": .null, "selection_revision": .integer(revision)])]),
                 "history": .null])
    }

    /// Catalog replies pass the Python-owned response schema, so they are real wire shapes;
    /// the abbreviated context and history replies bypass only wire-shape validation.
    static func reply(_ data: RecordingJSON, error: RecordingJSON = .null,
                      schema: String? = "recording-response") throws -> RecordingWireEnvelope {
        let body: RecordingJSON = .object(["data": data, "error": error, "meta": .object([
            "protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("fixture-request"),
            "process_generation": .string("synthetic-generation"), "state_revision": .integer(2),
            "event_sequence": .integer(3), "take_id": .null])])
        let validator = try schema.map { try Data(contentsOf: repository.appendingPathComponent("schemas/\($0).schema.json")) }
        return try RecordingWireEnvelope.decode(body.transportData(), schema: validator ?? Data(#"{"type":"object"}"#.utf8))
    }

    /// A service error envelope, as the response schema allows it.
    static func errorReply(_ code: String, _ message: String) throws -> RecordingWireEnvelope {
        try reply(.null, error: .object(["code": .string(code), "message": .string(message), "retryable": .boolean(false)]))
    }

    static func inventoryPage() -> RecordingJSON {
        .object(["event": .string("recovery-inventory"), "page": .object([
            "scan_generation": .string("scan-one"), "inventory_revision": .integer(1), "scan_state": .string("scanning"),
            "enumeration_complete": .boolean(false), "pending_count": .integer(0), "finding_count": .integer(0),
            "recovered_seal_count": .integer(0), "covered_store_revision": .null, "entries": .array([]),
            "next_cursor": .null, "error": .null])])
    }

    /// The command the workspace sends must pass the Python-owned request schema, including
    /// its exactly-one-catalog-form rule.
    private static func requireRequestShape(_ command: RecordingJSON) throws {
        let body: RecordingJSON = .object(["data": command, "error": .null, "meta": .object([
            "protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("fixture-request"),
            "process_generation": .string("synthetic-generation"), "state_revision": .integer(0),
            "event_sequence": .integer(0), "take_id": .null])])
        _ = try RecordingWireEnvelope.decode(body.transportData(), schema: Data(contentsOf:
            repository.appendingPathComponent("schemas/recording-service-request.schema.json")))
    }

    @Test @MainActor func theCatalogPrecedesTheSnapshotAndTheContextOpensByItsReference() async throws {
        let service = ScriptedService()
        // A restored context names the revision the new open-context must expect.
        service.durableRevision = 7
        let model = RecordingWorkspaceModel(bootstrap: service.requests, target: Self.target)
        #expect(model.prompts.isEmpty && model.catalog == nil && model.catalogReference == nil && model.catalogSHA256 == nil)
        #expect(model.promptPlaceholder == "Preparing catalog")
        let opened = await model.prepareContext()
        #expect(opened)
        #expect(service.commands == ["catalog", "snapshot", "open-context"])
        let catalogRequest = try #require(service.sent.first)
        #expect(catalogRequest.object?["operation_id"]?.string?.hasPrefix("catalog-") == true)
        try Self.requireRequestShape(catalogRequest)

        let open = try #require(service.sent.last?.object)
        #expect(open["catalog_reference"] == Self.reference())
        #expect(open["catalog"] == nil && open["catalog_sha256"] == nil, "the app never embeds a catalog")
        #expect(open["subject_id"] == .string("synthetic-subject") && open["capture_host"] == .string("synthetic-host"))
        #expect(open["capture_device"] == Self.target.captureDevice)
        #expect(open["expected_selection_revision"] == .integer(7) && open["new_setup"] == .boolean(false))
        #expect(open["pilot_approval_path"] == .null && open["pilot_approval_sha256"] == .null)
        try Self.requireRequestShape(.object(open))

        // The Library and every digest binding come from the delivered event.
        #expect(model.catalogReference == Self.reference() && model.catalogSHA256 == Self.digest)
        #expect(model.catalog?.object?["suite_id"] == .string("synthetic-store"))
        #expect(model.prompts.map(\.id) == ["room-tone-start", "reading-one", "reading-two"])
        #expect(model.prompts.allSatisfy { $0.catalogPurpose == "capture-pilot" })
        #expect(model.state.prompt?.id == "reading-one" && model.state.groupID == "group-store")
        #expect(model.state.contextReady && model.state.phase == .ready && model.state.errorCode == nil)
        #expect(model.state.selection.object?["catalog_sha256"] == .string(Self.digest))
        #expect(model.state.selection.object?["selection_revision"] == .integer(8))
        // History pages bind to the delivered digest only.
        model.handleService(try Self.reply(Self.historyPage(digest: String(repeating: "e", count: 64)), schema: nil))
        #expect(model.state.historyRevision == 0)
        model.handleService(try Self.reply(Self.historyPage(), schema: nil))
        #expect(model.state.historyRevision == 3)
    }

    @Test @MainActor func aRefusedCatalogIsShownVerbatimAndNothingElseIsRequested() async throws {
        let service = ScriptedService()
        service.refusedCatalogs = 1
        let model = RecordingWorkspaceModel(bootstrap: service.requests, target: Self.target)
        let opened = await model.prepareContext()
        #expect(!opened)
        #expect(service.commands == ["catalog"], "no snapshot or open-context follows a refused catalog")
        #expect(model.state.status == Self.refusal && model.state.errorCode == "CORPUS.NO_ACTIVE_CATALOG")
        #expect(model.state.phase == .recoverableError && !model.state.contextReady)
        #expect(model.state.contextReason == Self.refusal)
        #expect(!model.canRecord && model.recordUnavailableReason == Self.refusal)
        #expect(model.prompts.isEmpty && model.catalog == nil && model.catalogReference == nil && model.catalogSHA256 == nil)
        #expect(model.state.prompt == nil && model.state.groupID == nil && model.labelOptions.isEmpty)
        // The header no longer promises a catalog that will not come on its own.
        #expect(model.promptPlaceholder == "Catalog unavailable")
        // Without a delivered catalog, no history page or cursor binds and Record does nothing.
        model.handleService(try Self.reply(Self.historyPage(), schema: nil))
        #expect(model.state.historyRevision == 0 && model.state.promptStatuses.isEmpty)
        let cursor = Self.contextEvent(operation: "context", promptID: "reading-one", digest: Self.digest, revision: 1).object?["cursor"] ?? .null
        #expect(throws: RecordingContractError.self) { try model.applyCursor(cursor, preserveTake: false) }
        model.record()
        #expect(model.state.phase == .recoverableError && model.state.activeOperationID == nil)
        #expect(service.commands == ["catalog"])
    }

    @Test @MainActor func mismatchedCatalogRepliesAreRefusedAndTheHeldCatalogNeverChanges() throws {
        let model = RecordingWorkspaceModel(bootstrap: ScriptedService().requests, target: Self.target)
        let catalog = Self.pilotCatalog()
        #expect(throws: RecordingContractError.self) {
            try model.applyCatalog(Self.catalogEvent("other-operation", catalog: catalog, reference: Self.reference()), operationID: "catalog-one")
        }
        #expect(throws: RecordingContractError.self) {
            try model.applyCatalog(Self.catalogEvent("catalog-one", catalog: catalog, reference: Self.reference(purpose: "capture-corpus")), operationID: "catalog-one")
        }
        #expect(throws: RecordingContractError.self) {
            try model.applyCatalog(Self.catalogEvent("catalog-one", catalog: catalog, reference: Self.reference(suiteID: "other-suite")), operationID: "catalog-one")
        }
        // One incomplete prompt refuses the whole Library.
        var incomplete = catalog.object ?? [:]
        incomplete["prompts"] = .array((catalog.object?["prompts"]?.array ?? []) + [.object(["prompt_id": .string("no-style"), "category": .string("neutral")])])
        #expect(throws: RecordingContractError.self) {
            try model.applyCatalog(Self.catalogEvent("catalog-one", catalog: .object(incomplete), reference: Self.reference()), operationID: "catalog-one")
        }
        #expect(model.prompts.isEmpty && model.catalog == nil && model.catalogReference == nil && model.catalogSHA256 == nil)

        try model.applyCatalog(Self.catalogEvent("catalog-one", catalog: catalog, reference: Self.reference()), operationID: "catalog-one")
        // The same delivery again is harmless; another catalog within one service process is not.
        try model.applyCatalog(Self.catalogEvent("catalog-two", catalog: catalog, reference: Self.reference()), operationID: "catalog-two")
        let next = Self.reference(digest: String(repeating: "e", count: 64), version: 2)
        do {
            try model.applyCatalog(Self.catalogEvent("catalog-three", catalog: catalog, reference: next), operationID: "catalog-three")
            Issue.record("A different catalog replaced the held one")
        } catch let error as RecordingContractError {
            #expect(error.message.contains("restart the app"), "\(error.message)")
        }
        #expect(model.catalogReference == Self.reference() && model.catalogSHA256 == Self.digest && model.prompts.count == 3)
    }

    @Test @MainActor func aDeliveredPhysicalTestCatalogDrivesItsChips() async throws {
        let service = ScriptedService()
        service.catalog = Self.physicalTestCatalog()
        service.reference = Self.reference(purpose: "physical-test", suiteID: "cole-physical-test-synthetic-store")
        let model = RecordingWorkspaceModel(bootstrap: service.requests, target: Self.target)
        let opened = await model.prepareContext()
        #expect(opened)
        #expect(model.catalog?.object?["purpose"] == .string("physical-test"))
        #expect(model.prompts.allSatisfy { $0.catalogPurpose == "physical-test" && $0.effectiveLabels.contains("test") && $0.pinnedLabelCount == 2 })
        #expect(model.labelOptions == ["room-tone", "test", "neutral", "procedure", "device-loss"])
    }

    @Test @MainActor func aPilotApprovalBuildRefusesANonCorpusCatalogNamingBothPurposes() async throws {
        let service = ScriptedService()
        let model = RecordingWorkspaceModel(bootstrap: service.requests, target: Self.approvedTarget)
        let opened = await model.prepareContext()
        #expect(!opened)
        #expect(service.commands == ["catalog"], "no open-context is sent that could only be refused")
        #expect(model.state.errorCode == "RECORDING.GATE_NOT_READY" && model.state.phase == .recoverableError)
        #expect(model.state.status.contains("built for capture-corpus with a pilot approval"), "\(model.state.status)")
        #expect(model.state.status.contains("active catalog synthetic-store version 1 is capture-pilot"), "\(model.state.status)")
        #expect(model.state.contextReason == model.state.status && !model.canRecord)
        #expect(model.catalogReference == nil && model.prompts.isEmpty)

        // A capture-corpus catalog opens with the configured approval.
        let corpus = ScriptedService()
        var suite = Self.pilotCatalog().object ?? [:]
        suite["purpose"] = .string("capture-corpus")
        corpus.catalog = .object(suite)
        corpus.reference = Self.reference(purpose: "capture-corpus")
        let approved = RecordingWorkspaceModel(bootstrap: corpus.requests, target: Self.approvedTarget)
        let openedCorpus = await approved.prepareContext()
        #expect(openedCorpus)
        let open = try #require(corpus.sent.last?.object)
        #expect(open["pilot_approval_path"] == .string("provenance/pilot-gate.json"))
        try Self.requireRequestShape(.object(open))
    }

    @Test @MainActor func reconnectNamesTheHeldReferenceAndNeverRequestsTheCatalogAgain() async throws {
        let service = ScriptedService()
        service.durableRevision = 7
        let model = RecordingWorkspaceModel(bootstrap: service.requests, target: Self.target)
        let opened = await model.prepareContext()
        #expect(opened)
        model.requireAudioReconnect(after: .deviceUnavailable)
        model.state.deviceReady = true // The fixture has no helper to prepare the device.
        #expect(model.canReconnectAudio)
        await model.reconnectAudio()
        #expect(service.commands == ["catalog", "snapshot", "open-context", "open-context"])
        let reopen = try #require(service.sent.last?.object)
        #expect(reopen["catalog_reference"] == Self.reference() && reopen["catalog"] == nil)
        #expect(reopen["new_setup"] == .boolean(true) && reopen["expected_selection_revision"] == .integer(8))
        #expect(reopen["setup_description"] == .string(RecordingAudioReconnectCause.deviceUnavailable.contextDescription))
        try Self.requireRequestShape(.object(reopen))
        #expect(!model.audioReconnectRequired && model.state.contextReady && model.state.errorCode == nil)
    }

    /// Cole's library already has a cursor, so the service accepts only its exact revision.
    @Test(arguments: ["catalog", "open-context"])
    @MainActor func aReconnectAfterARefusedLaunchFinishesTheBootstrapAtTheDurableRevision(refused: String) async throws {
        let service = ScriptedService()
        service.durableRevision = 7
        if refused == "catalog" { service.refusedCatalogs = 1 } else { service.refusedOpens = 1 }
        let model = RecordingWorkspaceModel(bootstrap: service.requests, target: Self.target)
        model.recoverySnapshotRequest = { [service] _ in
            service.inventoryRequests += 1
            return try Self.reply(Self.inventoryPage(), schema: nil)
        }
        let opened = await model.prepareContext()
        #expect(!opened && model.state.phase == .recoverableError)
        #expect(model.promptPlaceholder == (refused == "catalog" ? "Catalog unavailable" : "Recording context unavailable"))
        let launch = refused == "catalog" ? ["catalog"] : ["catalog", "snapshot", "open-context"]
        #expect(service.commands == launch)
        model.requireAudioReconnect(after: .deviceUnavailable)
        model.state.deviceReady = true
        #expect(model.canReconnectAudio)
        await model.reconnectAudio()
        // Nothing applied a cursor, so the durable snapshot names the revision to expect.
        let retry = (refused == "catalog" ? ["catalog"] : []) + ["snapshot", "open-context"]
        #expect(service.commands == launch + retry)
        let reopen = try #require(service.sent.last?.object)
        #expect(reopen["catalog_reference"] == Self.reference() && reopen["expected_selection_revision"] == .integer(7))
        #expect(reopen["new_setup"] == .boolean(true))
        try Self.requireRequestShape(.object(reopen))
        #expect(model.catalogSHA256 == Self.digest && model.prompts.count == 3)
        #expect(model.state.prompt?.id == "reading-one" && model.state.contextReady && model.state.phase == .ready)
        #expect(model.state.selection.object?["selection_revision"] == .integer(8))
        #expect(model.state.errorCode == nil && !model.audioReconnectRequired)
        // The launch bootstrap's remaining step, the recovery inventory, ran once.
        #expect(service.inventoryRequests == 1 && model.recoveryPagesLoaded == 1 && model.recoveryBootstrapFinished)
        // A later reconnect expects the applied revision and loads nothing again.
        model.requireAudioReconnect(after: .deviceUnavailable)
        model.state.deviceReady = true
        await model.reconnectAudio()
        #expect(service.commands == launch + retry + ["open-context"])
        #expect(service.sent.last?.object?["expected_selection_revision"] == .integer(8))
        #expect(model.state.contextReady && service.inventoryRequests == 1)
    }

    @Test @MainActor func aReconnectThatExpectsAStaleRevisionIsRefusedAsTheServiceRefusesIt() async throws {
        let service = ScriptedService()
        let model = RecordingWorkspaceModel(bootstrap: service.requests, target: Self.target)
        let opened = await model.prepareContext()
        #expect(opened)
        // Another writer moved the durable cursor; the applied revision is now stale.
        service.durableRevision = 5
        model.requireAudioReconnect(after: .deviceUnavailable)
        model.state.deviceReady = true
        await model.reconnectAudio()
        #expect(model.state.errorCode == "RECORDING.STALE_REVISION" && model.state.status == Self.staleRevision)
        #expect(model.audioReconnectRequired && model.state.phase == .recoverableError)
    }
}
