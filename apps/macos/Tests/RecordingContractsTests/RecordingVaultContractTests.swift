import Foundation
@testable import RecordingContracts
import Testing

/// Library-vault status against the committed generated schemas: take-less, each state
/// carrying exactly its own evidence, and the storage codes travelling in the event's own
/// field rather than in the wire error code. Mirrors
/// tests/test_recording_contracts.py::vault_cases.
@Suite struct RecordingVaultContractTests {
    private let repository = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent()
    private let operationID = "op-vault-status"
    /// Mirrors storage_ports.StorageErrorCode, which Python holds equal to the wire
    /// VaultStorageErrorCode; no member of it is a RecordingError code.
    private let storageCodes = [
        "STORAGE.UNAVAILABLE", "STORAGE.TIMEOUT", "STORAGE.UNAUTHORIZED", "STORAGE.NOT_FOUND",
        "STORAGE.CONFLICT", "STORAGE.TOO_LARGE", "STORAGE.QUOTA", "STORAGE.INTEGRITY",
        "STORAGE.IDENTITY_MISMATCH", "STORAGE.NOT_APPROVED", "STORAGE.NOT_BOUND", "STORAGE.PROTOCOL",
    ]

    private func validator(_ name: String) throws -> RecordingSchemaValidator {
        try RecordingSchemaValidator(preparing: Data(contentsOf: repository.appendingPathComponent("schemas/\(name).schema.json")))
    }

    private func accepts(_ validator: RecordingSchemaValidator, _ value: [String: RecordingJSON]) throws -> Bool {
        do { _ = try validator.validate(RecordingJSON.object(value).transportData()); return true }
        catch is RecordingContractError { return false }
    }

    private func meta(takeID: RecordingJSON = .null) -> RecordingJSON {
        .object(["protocol_version": .string("cole-voice-lab.recording/v1"), "request_id": .string("synthetic-request"),
                 "process_generation": .string("synthetic-gen"), "state_revision": .integer(2),
                 "event_sequence": .integer(1), "take_id": takeID])
    }

    private func response(data: RecordingJSON = .null, error: RecordingJSON = .null, takeID: RecordingJSON = .null) -> [String: RecordingJSON] {
        ["data": data, "error": error, "meta": meta(takeID: takeID)]
    }

    private func request(_ data: [String: RecordingJSON]) -> [String: RecordingJSON] {
        ["data": .object(data), "error": .null, "meta": meta()]
    }

    /// An up-to-date vault at generation 7, with `changes` applied.
    private func status(_ changes: [String: RecordingJSON] = [:]) -> [String: RecordingJSON] {
        ["event": .string("vault-status"), "operation_id": .string(operationID), "state": .string("up-to-date"),
         "waiting_files": .integer(0), "unreachable_since": .null, "last_pushed_at": .string("2026-09-17T13:55:00Z"),
         "generation": .integer(7), "code": .null, "message": .null].merging(changes) { $1 }
    }

    private var off: [String: RecordingJSON] {
        status(["state": .string("off"), "last_pushed_at": .null, "generation": .integer(0)])
    }

    private var unreachable: [String: RecordingJSON] {
        status(["state": .string("unreachable"), "unreachable_since": .string("2026-09-17T14:02:00Z"),
                "code": .string("STORAGE.UNAVAILABLE"), "message": .string("the vault did not answer")])
    }

    private var failed: [String: RecordingJSON] {
        status(["state": .string("failed"), "last_pushed_at": .null, "generation": .integer(0),
                "code": .string("STORAGE.NOT_APPROVED"), "message": .string("the destination approval expired on 2026-09-16")])
    }

    @Test func everyVaultStateCarriesItsOwnEvidence() throws {
        let responses = try validator("recording-response")
        let accepted: [(String, [String: RecordingJSON])] = [
            ("off", off),
            ("up to date", status()),
            ("copying", status(["state": .string("copying")])),
            ("waiting with a count", status(["state": .string("waiting"), "waiting_files": .integer(12)])),
            ("waiting for an owner", status(["state": .string("waiting"), "message": .string("waiting for fine-tune-runs")])),
            ("waiting before the first push", status(["state": .string("waiting"), "last_pushed_at": .null, "generation": .integer(0)])),
            ("unreachable", unreachable),
            ("failed", failed),
        ]
        for (name, event) in accepted {
            #expect(try accepts(responses, response(data: .object(event))), "\(name) is a valid vault status")
        }
        let refused: [(String, [String: RecordingJSON])] = [
            ("no state", status(["state": .null])),
            ("an unknown state", status(["state": .string("paused")])),
            ("off with a generation", off.merging(["generation": .integer(1)]) { $1 }),
            ("off with files waiting", off.merging(["waiting_files": .integer(3)]) { $1 }),
            ("off with a push time", off.merging(["last_pushed_at": .string("2026-09-17T13:55:00Z")]) { $1 }),
            ("unreachable without a time", unreachable.merging(["unreachable_since": .null]) { $1 }),
            ("unreachable since while up to date", status(["unreachable_since": .string("2026-09-17T14:02:00Z")])),
            ("a time without a zone", unreachable.merging(["unreachable_since": .string("2026-09-17T14:02:00")]) { $1 }),
            ("failed without a code", failed.merging(["code": .null]) { $1 }),
            ("failed without a message", failed.merging(["message": .null]) { $1 }),
            ("waiting with a code", status(["state": .string("waiting"), "code": .string("STORAGE.INTEGRITY"),
                                            "message": .string("a blob does not match its digest")])),
            ("a code while up to date", status(["code": .string("STORAGE.TIMEOUT")])),
            ("an unknown code", failed.merging(["code": .string("STORAGE.NOPE")]) { $1 }),
            ("a message while up to date", status(["message": .string("all copied")])),
            ("a message while copying", status(["state": .string("copying"), "message": .string("copying")])),
            ("a blank message", failed.merging(["message": .string("")]) { $1 }),
            ("a message over its bound", failed.merging(["message": .string(String(repeating: "x", count: 1025))]) { $1 }),
            ("a negative waiting count", status(["state": .string("waiting"), "waiting_files": .integer(-1)])),
            ("a fractional waiting count", status(["state": .string("waiting"), "waiting_files": .number(12.5)])),
            ("a waiting count over its bound", status(["state": .string("waiting"), "waiting_files": .integer(1_000_000_001)])),
            ("a negative generation", status(["generation": .integer(-1)])),
            ("an unstable operation id", status(["operation_id": .string("Vault")])),
        ]
        for (name, event) in refused {
            #expect(try !accepts(responses, response(data: .object(event))), "\(name) must fail closed")
        }
        #expect(try !accepts(responses, response(data: .object(status()), takeID: .string("take-a"))), "a vault status binds no take")
    }

    @Test func statusIsRequestedWithoutATake() throws {
        let requests = try validator("recording-service-request")
        let ask: [String: RecordingJSON] = ["command": .string("vault-status"), "operation_id": .string(operationID)]
        #expect(try accepts(requests, request(ask)))
        #expect(try !accepts(requests, request(["command": .string("vault-status"), "operation_id": .string("Vault")])))
        #expect(try !accepts(requests, request(["command": .string("vault-status")])))
        #expect(try !accepts(requests, request(ask.merging(["native_unknown_field": .string("x")]) { $1 })))
    }

    @Test func storageCodesTravelInTheEventNotTheWireError() throws {
        let responses = try validator("recording-response")
        #expect(storageCodes.count == 12)
        for code in storageCodes {
            #expect(try accepts(responses, response(data: .object(failed.merging(["code": .string(code)]) { $1 }))),
                    "\(code) is a vault status code")
            let error = RecordingJSON.object(["code": .string(code), "message": .string("Synthetic"), "retryable": .boolean(false)])
            #expect(try !accepts(responses, response(error: error)), "\(code) is not a wire error code")
        }
    }
}
