import Foundation
@testable import RecordingContracts
import Testing

/// recording-group/v1 (embedded catalog) and v2 (catalog reference) against the committed
/// generated schemas, alone and nested in the unchanged v1 containers. Mirrors
/// tests/test_recording_contracts.py::recording_group_v2_cases.
@Suite struct RecordingGroupVersionContractTests {
    private let repository = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
    private let v1 = "cole-voice-lab.recording-group/v1"
    private let v2 = "cole-voice-lab.recording-group/v2"
    private let catalogSHA256 = String(repeating: "c", count: 64)
    private let opened = "2026-09-11T12:00:00Z"

    private func validator(_ name: String) throws -> RecordingSchemaValidator {
        try RecordingSchemaValidator(preparing: Data(contentsOf: repository.appendingPathComponent("schemas/\(name).schema.json")))
    }

    private func accepts(_ validator: RecordingSchemaValidator, _ value: [String: RecordingJSON]) throws -> Bool {
        do { _ = try validator.validate(RecordingJSON.object(value).transportData()); return true }
        catch is RecordingContractError { return false }
    }

    private var prompt: [String: RecordingJSON] {
        ["prompt_id": .string("synthetic-room"), "text": .string("Remain silent."), "category": .string("room-tone"),
         "style": .string("silence"), "expected_seconds": .number(1)]
    }

    private func catalog(purpose: String = "capture-pilot", prompt catalogPrompt: [String: RecordingJSON]? = nil) -> RecordingJSON {
        .object(["suite_id": .string("synthetic-pilot"), "purpose": .string(purpose), "prompts": .array([.object(catalogPrompt ?? prompt)])])
    }

    private func reference(purpose: String = "capture-pilot", digest: String? = nil) -> [String: RecordingJSON] {
        let digest = digest ?? catalogSHA256
        return ["catalog_id": .string("cole-corpus"), "catalog_version": .integer(1), "catalog_sha256": .string(digest),
                "snapshot_sha256": .string(String(repeating: "d", count: 64)), "suite_id": .string("synthetic-pilot"),
                "purpose": .string(purpose), "snapshot_path": .string("provenance/corpus/snapshots/\(digest).json")]
    }

    private var readyGate: RecordingJSON {
        .object(["state": .string("ready"), "approval_path": .string("provenance/pilot-gate.json"),
                 "approval_sha256": .string(String(repeating: "a", count: 64)),
                 "evidence_sha256": .string(String(repeating: "b", count: 64)), "verified_at": .string(opened)])
    }

    private func group(version: String? = nil, embedded: Bool = false, referenced: Bool = true,
                       purpose: String = "capture-pilot", gate: RecordingJSON? = nil,
                       catalogPrompt: [String: RecordingJSON]? = nil) -> [String: RecordingJSON] {
        var value: [String: RecordingJSON] = [
            "group_id": .string("synthetic-group"), "context_revision": .integer(0), "subject_id": .string("synthetic-subject"),
            "catalog_sha256": .string(catalogSHA256),
            "capture_device": .object(["backend": .string("coreaudio"), "device_id": .string("synthetic-uid"), "display_name": .string("Synthetic")]),
            "capture_host": .string("synthetic-host"), "opened_at": .string(opened), "reason": .string("app-start"),
            "gate": gate ?? .object(["state": .string("not-required")]),
            "room_tone_take_ids": .array([.string("synthetic-take")]),
        ]
        if let version { value["schema_version"] = .string(version) }
        if embedded { value["catalog"] = catalog(purpose: purpose, prompt: catalogPrompt) }
        if referenced { value["catalog_reference"] = .object(reference(purpose: purpose)) }
        return value
    }

    private func reservation(_ group: [String: RecordingJSON], prompt promptValue: [String: RecordingJSON]? = nil) -> [String: RecordingJSON] {
        ["take_id": .string("synthetic-take"), "operation_id": .string("synthetic-reserve"), "process_generation": .string("synthetic-gen"),
         "group": .object(group), "prompt": .object(promptValue ?? prompt), "requested_at": .string(opened),
         "partial_path": .string("tmp/recording-takes/synthetic-take/capture.partial.wav"),
         "original_path": .string("tmp/recording-takes/synthetic-take/original.wav")]
    }

    private func packageMap(_ group: [String: RecordingJSON], selected: [String: RecordingJSON]?,
                            entryPrompt: [String: RecordingJSON]? = nil) -> [String: RecordingJSON] {
        var value: [String: RecordingJSON] = [
            "session_id": .string("synthetic-session"), "manifest_sha256": .string(String(repeating: "5", count: 64)),
            "package_path": .string("tmp/capture/synthetic-session"), "group": .object(group),
            "entries": .array([.object([
                "take_id": .string("synthetic-take"), "group_id": .string("synthetic-group"), "subject_id": .string("synthetic-subject"),
                "catalog_sha256": .string(catalogSHA256), "prompt": .object(entryPrompt ?? prompt), "package_relative_path": .string("room-tone-start.wav"),
                "original_sha256": .string(String(repeating: "4", count: 64)), "recorded_at": .string(opened),
                "edit_identity_sha256": .string(String(repeating: "6", count: 64)),
            ])]),
            "registered_at": .string("2026-09-11T12:00:03Z"),
        ]
        if let selected { value["selected_suite"] = .object(selected) }
        return value
    }

    private func selectedSuite(purpose: String = "capture-pilot", text: String = "Remain silent.") -> [String: RecordingJSON] {
        var chosen = prompt
        chosen["text"] = .string(text)
        return selectedSuite(purpose: purpose, prompt: chosen)
    }

    private func selectedSuite(purpose: String = "capture-pilot", prompt chosen: [String: RecordingJSON]) -> [String: RecordingJSON] {
        ["suite_id": .string("synthetic-export-subset"), "purpose": .string(purpose), "prompts": .array([.object(chosen)])]
    }

    private func promptWith(_ changes: [String: RecordingJSON]) -> [String: RecordingJSON] {
        prompt.merging(changes) { $1 }
    }

    private func response(data: RecordingJSON = .null, error: RecordingJSON = .null) -> [String: RecordingJSON] {
        ["data": data, "error": error,
         "meta": .object(["request_id": .string("synthetic-request"), "process_generation": .string("synthetic-gen"),
                          "state_revision": .integer(2), "event_sequence": .integer(1), "take_id": .null])]
    }

    private func context(_ group: [String: RecordingJSON], promptID: String) -> RecordingJSON {
        .object(["event": .string("context"), "group": .object(group), "cursor": .object([
            "subject_id": .string("synthetic-subject"), "group_id": .string("synthetic-group"),
            "selection": .object(["catalog_sha256": .string(catalogSHA256), "prompt_id": .string(promptID), "take_id": .null,
                                  "selection_revision": .integer(1), "index_revision": .integer(1)]),
            "mode": .string("selected-prompt"), "complete": .boolean(false), "operation_id": .string("context-open"),
        ])])
    }

    @Test func groupFormsAreExclusiveAndTheReferenceCarriesThePurpose() throws {
        let groups = try validator("recording-group")
        #expect(try accepts(groups, group(version: v1, embedded: true, referenced: false)))
        #expect(try accepts(groups, group(embedded: true, referenced: false)))  // version defaults to v1
        #expect(try accepts(groups, group(version: v2)))
        #expect(try accepts(groups, group(version: v2, purpose: "capture-corpus", gate: readyGate)))
        var explicitNull = group(version: v2)
        explicitNull["catalog"] = .null
        #expect(try accepts(groups, explicitNull))
        var unbound = group(version: v2)
        unbound["catalog_reference"] = .object(reference(digest: String(repeating: "0", count: 64)))
        var unboundPath = reference()
        unboundPath["snapshot_path"] = .string("provenance/corpus/snapshots/other.json")
        var pathMismatch = group(version: v2)
        pathMismatch["catalog_reference"] = .object(unboundPath)
        var unknownKey = reference()
        unknownKey["native_extra"] = .boolean(true)
        var extraKey = group(version: v2)
        extraKey["catalog_reference"] = .object(unknownKey)
        let refused: [(String, [String: RecordingJSON])] = [
            ("v2 embeds both", group(version: v2, embedded: true)),
            ("v1 carries both", group(version: v1, embedded: true)),
            ("v2 catalog instead of reference", group(version: v2, embedded: true, referenced: false)),
            ("v1 reference instead of catalog", group(version: v1)),
            ("v2 neither", group(version: v2, referenced: false)),
            ("v1 neither", group(version: v1, referenced: false)),
            ("missing version reads as v1", group()),
            ("unknown version", group(version: "cole-voice-lab.recording-group/v3")),
            ("reference digest", unbound),
            ("corpus reference without gate", group(version: v2, purpose: "capture-corpus")),
            ("pilot reference with gate", group(version: v2, gate: readyGate)),
            ("snapshot path", pathMismatch),
            ("unknown reference key", extraKey),
        ]
        for (name, payload) in refused {
            #expect(try !accepts(groups, payload), "\(name) must fail closed")
        }
        let references = try validator("catalog-reference")
        #expect(try accepts(references, reference()))
        #expect(try !accepts(references, unboundPath))
    }

    @Test func containersApplyPromptMembershipOnlyToEmbeddedCatalogs() throws {
        let reservations = try validator("capture-reservation")
        var foreign = prompt
        foreign["prompt_id"] = .string("snapshot-only-prompt")
        #expect(try accepts(reservations, reservation(group(version: v2), prompt: foreign)))
        #expect(try !accepts(reservations, reservation(group(embedded: true, referenced: false), prompt: foreign)))
        #expect(try accepts(reservations, reservation(group(version: v2, purpose: "capture-corpus", gate: readyGate))))
        let unprepared = RecordingJSON.object(["state": .string("unprepared")])
        #expect(try !accepts(reservations, reservation(group(version: v2, purpose: "capture-corpus", gate: unprepared))))

        let packages = try validator("recording-package-map")
        let embedded = group(embedded: true, referenced: false)
        #expect(try accepts(packages, packageMap(embedded, selected: nil)))
        #expect(try accepts(packages, packageMap(embedded, selected: selectedSuite())))
        #expect(try !accepts(packages, packageMap(embedded, selected: selectedSuite(text: "Changed prompt"))))
        #expect(try accepts(packages, packageMap(group(version: v2), selected: selectedSuite())))
        #expect(try !accepts(packages, packageMap(group(version: v2), selected: nil)))
        #expect(try !accepts(packages, packageMap(group(version: v2), selected: selectedSuite(purpose: "capture-corpus"))))
        #expect(try !accepts(packages, packageMap(group(version: v2), selected: selectedSuite(text: "Changed prompt"))))

        let responses = try validator("recording-response")
        #expect(try accepts(responses, response(data: context(group(version: v2), promptID: "snapshot-only-prompt"))))
        #expect(try !accepts(responses, response(data: context(embedded, promptID: "snapshot-only-prompt"))))
        #expect(try accepts(responses, response(data: context(embedded, promptID: "synthetic-room"))))
        for code in ["CORPUS.NO_ACTIVE_CATALOG", "CORPUS.CATALOG_UNKNOWN", "CORPUS.SNAPSHOT_MISMATCH", "CORPUS.STORE_MISSING"] {
            let error = RecordingJSON.object(["code": .string(code), "message": .string("Synthetic corpus-store failure"), "retryable": .boolean(false)])
            #expect(try accepts(responses, response(error: error)), "\(code) is a wire error code")
        }
        let unlisted = RecordingJSON.object(["code": .string("CORPUS.PROMPT_DUPLICATE"), "message": .string("Synthetic"), "retryable": .boolean(false)])
        #expect(try !accepts(responses, response(error: unlisted)))
    }

    private func serviceRequest(_ data: [String: RecordingJSON], takeID: RecordingJSON = .null) -> [String: RecordingJSON] {
        ["data": .object(data), "error": .null,
         "meta": .object(["protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("synthetic-request"),
                          "process_generation": .string("synthetic-gen"), "state_revision": .integer(2),
                          "event_sequence": .integer(1), "take_id": takeID])]
    }

    private func openContext(referenced: Bool = true, embedded: Bool = false, digest: Bool? = nil,
                             purpose: String = "capture-pilot", approved: Bool = false) -> [String: RecordingJSON] {
        var value: [String: RecordingJSON] = [
            "command": .string("open-context"), "operation_id": .string("context-open"), "subject_id": .string("synthetic-subject"),
            "capture_device": .object(["backend": .string("coreaudio"), "device_id": .string("synthetic-uid"), "display_name": .string("Synthetic")]),
            "capture_host": .string("synthetic-host"),
        ]
        if referenced { value["catalog_reference"] = .object(reference(purpose: purpose)) }
        if embedded { value["catalog"] = catalog(purpose: purpose) }
        if digest ?? embedded { value["catalog_sha256"] = .string(catalogSHA256) }
        if approved {
            value["pilot_approval_path"] = .string("provenance/pilot-gate.json")
            value["pilot_approval_sha256"] = .string(String(repeating: "a", count: 64))
        }
        return value
    }

    @Test func openContextNamesExactlyOneCatalogFormAndLegacyContinuationEmbedsIt() throws {
        let requests = try validator("recording-service-request")
        #expect(try accepts(requests, serviceRequest(openContext())))
        #expect(try accepts(requests, serviceRequest(openContext(purpose: "capture-corpus", approved: true))))
        #expect(try accepts(requests, serviceRequest(openContext(referenced: false, embedded: true))))
        var explicitNull = openContext()
        explicitNull["catalog"] = .null
        explicitNull["catalog_sha256"] = .null
        #expect(try accepts(requests, serviceRequest(explicitNull)))
        let refusedContexts: [(String, [String: RecordingJSON])] = [
            ("both forms", openContext(embedded: true)),
            ("reference and digest", openContext(digest: true)),
            ("neither form", openContext(referenced: false)),
            ("catalog without digest", openContext(referenced: false, embedded: true, digest: false)),
            ("digest without catalog", openContext(referenced: false, digest: true)),
            ("pilot approval on a pilot reference", openContext(approved: true)),
            ("pilot approval on an embedded pilot catalog", openContext(referenced: false, embedded: true, approved: true)),
        ]
        for (name, payload) in refusedContexts {
            #expect(try !accepts(requests, serviceRequest(payload)), "\(name) must fail closed")
        }
        func legacy(_ context: [String: RecordingJSON]) -> [String: RecordingJSON] {
            var nested = context
            nested["operation_id"] = .string("continue-legacy")
            nested["new_setup"] = .boolean(true)
            return serviceRequest(["command": .string("continue-legacy-recording"), "operation_id": .string("continue-legacy"),
                                   "session_id": .string("unfinished-session"),
                                   "session_evidence_sha256": .string(String(repeating: "a", count: 64)), "context": .object(nested)])
        }
        #expect(try accepts(requests, legacy(openContext(referenced: false, embedded: true))))
        #expect(try !accepts(requests, legacy(openContext())))

        let catalogCommand: [String: RecordingJSON] = ["command": .string("catalog"), "operation_id": .string("op-catalog")]
        #expect(try accepts(requests, serviceRequest(catalogCommand)))
        #expect(try !accepts(requests, serviceRequest(catalogCommand, takeID: .string("synthetic-take"))))
        #expect(try !accepts(requests, serviceRequest(["command": .string("catalog")])))
    }

    @Test func catalogEventRepeatsItsReferenceIdentity() throws {
        let responses = try validator("recording-response")
        func event(suiteID: String = "synthetic-pilot", purpose: String = "capture-pilot",
                   referencePurpose: String = "capture-pilot") -> RecordingJSON {
            var suite = catalog(purpose: purpose).object ?? [:]
            suite["suite_id"] = .string(suiteID)
            return .object(["event": .string("catalog"), "operation_id": .string("op-catalog"),
                            "catalog": .object(suite), "reference": .object(reference(purpose: referencePurpose))])
        }
        // The suite digest is Python evidence; Swift checks the identity the reference repeats.
        #expect(try accepts(responses, response(data: event())))
        #expect(try !accepts(responses, response(data: event(suiteID: "another-suite"))))
        #expect(try !accepts(responses, response(data: event(referencePurpose: "capture-corpus"))))
        var withTake = response(data: event())
        var meta = withTake["meta"]?.object ?? [:]
        meta["take_id"] = .string("synthetic-take")
        withTake["meta"] = .object(meta)
        #expect(try !accepts(responses, withTake))
    }

    @Test func selectedSuiteOfAnEmbeddedCatalogKeepsItsPurpose() throws {
        let packages = try validator("recording-package-map")
        let embedded = group(embedded: true, referenced: false)
        #expect(try accepts(packages, packageMap(embedded, selected: selectedSuite(purpose: "capture-pilot"))))
        // A valid but different purpose fails only the selected-suite rule.
        #expect(try !accepts(packages, packageMap(embedded, selected: selectedSuite(purpose: "capture-corpus"))))
    }

    @Test func promptComparisonsFollowPythonValueEquality() throws {
        let composed = "Remain sil\u{E9}nt."
        let decomposed = "Remain sile\u{301}nt."
        #expect(RecordingJSON.string(composed) == .string(decomposed))  // Swift String equivalence
        #expect(!RecordingSemanticValidator.pythonEqual(.string(composed), .string(decomposed)))
        #expect(RecordingSemanticValidator.pythonEqual(.integer(1), .number(1)))
        #expect(RecordingSemanticValidator.pythonEqual(.number(1), .integer(1)))
        #expect(!RecordingSemanticValidator.pythonEqual(.integer(1), .number(1.5)))
        #expect(!RecordingSemanticValidator.pythonEqual(.boolean(true), .integer(1)))
        #expect(!RecordingSemanticValidator.pythonEqual(.integer(2), .integer(3)))
        #expect(!RecordingSemanticValidator.pythonEqual(.number(2), .number(3)))
        #expect(RecordingSemanticValidator.pythonEqual(.boolean(false), .boolean(false)))
        #expect(RecordingSemanticValidator.pythonEqual(.null, .null))
        #expect(!RecordingSemanticValidator.pythonEqual(.null, .string("")))
        #expect(RecordingSemanticValidator.pythonEqual(.object(["a": .array([.integer(2), .null])]), .object(["a": .array([.number(2), .null])])))
        #expect(!RecordingSemanticValidator.pythonEqual(.object(["a": .null]), .object(["b": .null])))
        #expect(!RecordingSemanticValidator.pythonEqual(.object(["a": .null]), .object([:])))
        #expect(!RecordingSemanticValidator.pythonEqual(.array([.null]), .array([])))

        // An integer token in a float field is the same prompt, as in Python.
        let integerSeconds = promptWith(["expected_seconds": .integer(1)])
        let packages = try validator("recording-package-map")
        let embedded = group(embedded: true, referenced: false)
        #expect(try accepts(packages, packageMap(embedded, selected: selectedSuite(prompt: integerSeconds))))
        #expect(try accepts(packages, packageMap(embedded, selected: nil, entryPrompt: integerSeconds)))
        #expect(try accepts(packages, packageMap(group(version: v2), selected: selectedSuite(prompt: integerSeconds))))
        let reservations = try validator("capture-reservation")
        #expect(try accepts(reservations, reservation(embedded, prompt: integerSeconds)))

        // Canonically equivalent but different text is a different prompt, as in Python.
        let nfc = promptWith(["text": .string(composed)])
        let nfd = promptWith(["text": .string(decomposed)])
        let composedGroup = group(embedded: true, referenced: false, catalogPrompt: nfc)
        #expect(try accepts(packages, packageMap(composedGroup, selected: selectedSuite(prompt: nfc), entryPrompt: nfc)))
        #expect(try !accepts(packages, packageMap(composedGroup, selected: selectedSuite(prompt: nfd), entryPrompt: nfc)))
        #expect(try !accepts(packages, packageMap(composedGroup, selected: nil, entryPrompt: nfd)))
        #expect(try !accepts(packages, packageMap(group(version: v2), selected: selectedSuite(prompt: nfd), entryPrompt: nfc)))
        #expect(try accepts(reservations, reservation(composedGroup, prompt: nfc)))
        #expect(try !accepts(reservations, reservation(composedGroup, prompt: nfd)))
    }
}
